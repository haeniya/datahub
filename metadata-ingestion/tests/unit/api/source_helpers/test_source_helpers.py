import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Union
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import datahub.metadata.schema_classes as models
from datahub.configuration.time_window_config import BaseTimeWindowConfig
from datahub.emitter.mce_builder import (
    make_container_urn,
    make_dataplatform_instance_urn,
    make_dataset_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.auto_work_units.auto_dataset_properties_aspect import (
    auto_patch_last_modified,
)
from datahub.ingestion.api.source_helpers import (
    _prepend_platform_instance,
    auto_browse_path_v2,
    auto_empty_dataset_usage_statistics,
    auto_lowercase_urns,
    auto_status_aspect,
    auto_workunit,
    create_dataset_props_patch_builder,
)
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    OperationTypeClass,
    TimeStampClass,
)
from datahub.specific.dataset import DatasetPatchBuilder

_base_metadata: List[
    Union[MetadataChangeProposalWrapper, models.MetadataChangeEventClass]
] = [
    MetadataChangeProposalWrapper(
        entityUrn="urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a",
        aspect=models.ContainerPropertiesClass(
            name="test",
        ),
    ),
    MetadataChangeProposalWrapper(
        entityUrn="urn:li:container:108e111aa1d250dd52e0fd5d4b307b12",
        aspect=models.StatusClass(removed=True),
    ),
    models.MetadataChangeEventClass(
        proposedSnapshot=models.DatasetSnapshotClass(
            urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.staffing,PROD)",
            aspects=[
                models.DatasetPropertiesClass(
                    customProperties={
                        "key": "value",
                    },
                ),
            ],
        ),
    ),
    models.MetadataChangeEventClass(
        proposedSnapshot=models.DatasetSnapshotClass(
            urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.hospital_beds,PROD)",
            aspects=[
                models.StatusClass(removed=True),
            ],
        ),
    ),
]


def test_auto_workunit():
    wu = list(auto_workunit(_base_metadata))
    assert all(isinstance(w, MetadataWorkUnit) for w in wu)

    ids = [w.id for w in wu]
    assert ids == [
        "urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a-containerProperties",
        "urn:li:container:108e111aa1d250dd52e0fd5d4b307b12-status",
        "urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.staffing,PROD)/mce",
        "urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.hospital_beds,PROD)/mce",
    ]


def test_auto_status_aspect():
    initial_wu = list(auto_workunit(_base_metadata))

    expected = [
        *initial_wu,
        *list(
            auto_workunit(
                [
                    MetadataChangeProposalWrapper(
                        entityUrn="urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a",
                        aspect=models.StatusClass(removed=False),
                    ),
                    MetadataChangeProposalWrapper(
                        entityUrn="urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.staffing,PROD)",
                        aspect=models.StatusClass(removed=False),
                    ),
                ]
            )
        ),
    ]
    assert list(auto_status_aspect(initial_wu)) == expected


def _create_container_aspects(
    d: Dict[str, Any],
    other_aspects: Dict[str, List[models._Aspect]] = {},
    root: bool = True,
) -> Iterable[MetadataWorkUnit]:
    for k, v in d.items():
        urn = make_container_urn(k)
        yield MetadataChangeProposalWrapper(
            entityUrn=urn, aspect=models.StatusClass(removed=False)
        ).as_workunit()

        for aspect in other_aspects.pop(k, []):
            yield MetadataChangeProposalWrapper(
                entityUrn=urn, aspect=aspect
            ).as_workunit()

        for child in list(v):
            yield MetadataChangeProposalWrapper(
                entityUrn=make_container_urn(child),
                aspect=models.ContainerClass(container=urn),
            ).as_workunit()
        if isinstance(v, dict):
            yield from _create_container_aspects(
                v, other_aspects=other_aspects, root=False
            )

    if root:
        for k, v in other_aspects.items():
            for aspect in v:
                yield MetadataChangeProposalWrapper(
                    entityUrn=make_container_urn(k), aspect=aspect
                ).as_workunit()


def _make_container_browse_path_entries(
    path: List[str],
) -> List[models.BrowsePathEntryClass]:
    return [
        models.BrowsePathEntryClass(id=make_container_urn(s), urn=make_container_urn(s))
        for s in path
    ]


def _make_browse_path_entries(path: List[str]) -> List[models.BrowsePathEntryClass]:
    return [models.BrowsePathEntryClass(id=s, urn=None) for s in path]


def prepend_platform_instance(
    path: List[models.BrowsePathEntryClass],
) -> List[models.BrowsePathEntryClass]:
    platform = "platform"
    instance = "instance"
    return _prepend_platform_instance(path, platform, instance)


def _get_browse_paths_from_wu(
    stream: Iterable[MetadataWorkUnit],
) -> Dict[str, List[models.BrowsePathEntryClass]]:
    paths = {}
    for wu in stream:
        browse_path_v2 = wu.get_aspect_of_type(models.BrowsePathsV2Class)
        if browse_path_v2:
            name = wu.get_urn().split(":")[-1]
            paths[name] = browse_path_v2.path
    return paths


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_by_container_hierarchy(telemetry_ping_mock):
    structure = {
        "one": {
            "a": {"i": ["1", "2", "3"], "ii": ["4"]},
            "b": {"iii": ["5", "6"]},
        },
        "two": {
            "c": {"iv": [], "v": ["7", "8"]},
        },
        "three": {"d": {}},
        "four": {},
    }

    wus = list(auto_status_aspect(_create_container_aspects(structure)))
    assert (  # Sanity check
        sum(bool(wu.get_aspect_of_type(models.StatusClass)) for wu in wus) == 21
    )

    new_wus = list(auto_browse_path_v2(wus))
    assert not telemetry_ping_mock.call_count, telemetry_ping_mock.call_args_list
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 21
    )

    paths = _get_browse_paths_from_wu(new_wus)
    assert paths["one"] == []
    assert (
        paths["7"]
        == paths["8"]
        == _make_container_browse_path_entries(["two", "c", "v"])
    )
    assert paths["d"] == _make_container_browse_path_entries(["three"])
    assert paths["i"] == _make_container_browse_path_entries(["one", "a"])

    # Check urns emitted on demand -- not all at end
    for urn in {wu.get_urn() for wu in new_wus}:
        try:
            idx = next(
                i
                for i, wu in enumerate(new_wus)
                if wu.get_aspect_of_type(models.ContainerClass) and wu.get_urn() == urn
            )
        except StopIteration:
            idx = next(
                i
                for i, wu in enumerate(new_wus)
                if wu.get_aspect_of_type(models.StatusClass) and wu.get_urn() == urn
            )
        assert new_wus[idx + 1].get_aspect_of_type(
            models.BrowsePathsV2Class
        ) or new_wus[idx + 2].get_aspect_of_type(models.BrowsePathsV2Class)


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_ignores_urns_already_with(telemetry_ping_mock):
    structure = {"a": {"b": {"c": {"d": ["e"]}}}}

    wus = [
        *auto_status_aspect(
            _create_container_aspects(
                structure,
                other_aspects={
                    "f": [
                        models.BrowsePathsClass(paths=["/one/two"]),
                        models.BrowsePathsV2Class(
                            path=_make_browse_path_entries(["my", "path"])
                        ),
                    ],
                    "c": [
                        models.BrowsePathsV2Class(
                            path=_make_container_browse_path_entries(["custom", "path"])
                        )
                    ],
                },
            ),
        )
    ]
    new_wus = list(auto_browse_path_v2(wus))
    assert not telemetry_ping_mock.call_count, telemetry_ping_mock.call_args_list
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 6
    )

    paths = _get_browse_paths_from_wu(new_wus)
    assert paths["a"] == []
    assert paths["c"] == _make_container_browse_path_entries(["custom", "path"])
    assert paths["f"] == _make_browse_path_entries(["my", "path"])
    assert paths["d"] == _make_container_browse_path_entries(["custom", "path", "c"])
    assert paths["e"] == _make_container_browse_path_entries(
        ["custom", "path", "c", "d"]
    )


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_with_platform_instance_and_source_browse_path_v2(
    telemetry_ping_mock,
):
    structure = {"a": {"b": {"c": {"d": ["e"]}}}}

    platform = "platform"
    instance = "instance"

    wus = [
        *auto_status_aspect(
            _create_container_aspects(
                structure,
                other_aspects={
                    "a": [
                        models.BrowsePathsV2Class(
                            path=_make_browse_path_entries(["my", "path"]),
                        ),
                    ],
                },
            ),
        )
    ]
    new_wus = list(
        auto_browse_path_v2(wus, platform=platform, platform_instance=instance)
    )
    assert not telemetry_ping_mock.call_count, telemetry_ping_mock.call_args_list
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 5
    )

    paths = _get_browse_paths_from_wu(new_wus)
    assert paths["a"] == prepend_platform_instance(
        _make_browse_path_entries(["my", "path"]),
    )
    assert paths["b"] == prepend_platform_instance(
        [
            *_make_browse_path_entries(["my", "path"]),
            *_make_container_browse_path_entries(["a"]),
        ],
    )
    assert paths["c"] == prepend_platform_instance(
        [
            *_make_browse_path_entries(["my", "path"]),
            *_make_container_browse_path_entries(["a", "b"]),
        ],
    )
    assert paths["d"] == prepend_platform_instance(
        [
            *_make_browse_path_entries(["my", "path"]),
            *_make_container_browse_path_entries(["a", "b", "c"]),
        ],
    )
    assert paths["e"] == prepend_platform_instance(
        [
            *_make_browse_path_entries(["my", "path"]),
            *_make_container_browse_path_entries(["a", "b", "c", "d"]),
        ],
    )


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_legacy_browse_path(telemetry_ping_mock):
    platform = "platform"
    env = "PROD"
    wus = [
        MetadataChangeProposalWrapper(
            entityUrn=make_dataset_urn(platform, "dataset-1", env),
            aspect=models.BrowsePathsClass(["/one/two"]),
        ).as_workunit(),
        MetadataChangeProposalWrapper(
            entityUrn=make_dataset_urn(platform, "dataset-2", env),
            aspect=models.BrowsePathsClass([f"/{platform}/{env}/something"]),
        ).as_workunit(),
        MetadataChangeProposalWrapper(
            entityUrn=make_dataset_urn(platform, "dataset-3", env),
            aspect=models.BrowsePathsClass([f"/{platform}/one/two"]),
        ).as_workunit(),
    ]
    new_wus = list(auto_browse_path_v2(wus, drop_dirs=["platform", "PROD", "unused"]))
    assert not telemetry_ping_mock.call_count, telemetry_ping_mock.call_args_list
    assert len(new_wus) == 6
    paths = _get_browse_paths_from_wu(new_wus)
    assert (
        paths["platform,dataset-1,PROD)"]
        == paths["platform,dataset-3,PROD)"]
        == _make_browse_path_entries(["one", "two"])
    )
    assert paths["platform,dataset-2,PROD)"] == _make_browse_path_entries(["something"])


def test_auto_lowercase_aspects():
    mcws = auto_workunit(
        [
            MetadataChangeProposalWrapper(
                entityUrn=make_dataset_urn(
                    "bigquery", "myProject.mySchema.myTable", "PROD"
                ),
                aspect=models.DatasetKeyClass(
                    "urn:li:dataPlatform:bigquery", "myProject.mySchema.myTable", "PROD"
                ),
            ),
            MetadataChangeProposalWrapper(
                entityUrn="urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a",
                aspect=models.ContainerPropertiesClass(
                    name="test",
                ),
            ),
            models.MetadataChangeEventClass(
                proposedSnapshot=models.DatasetSnapshotClass(
                    urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-Public-Data.Covid19_Aha.staffing,PROD)",
                    aspects=[
                        models.DatasetPropertiesClass(
                            customProperties={
                                "key": "value",
                            },
                        ),
                    ],
                ),
            ),
        ]
    )

    expected = [
        *list(
            auto_workunit(
                [
                    MetadataChangeProposalWrapper(
                        entityUrn="urn:li:dataset:(urn:li:dataPlatform:bigquery,myproject.myschema.mytable,PROD)",
                        aspect=models.DatasetKeyClass(
                            "urn:li:dataPlatform:bigquery",
                            "myProject.mySchema.myTable",
                            "PROD",
                        ),
                    ),
                    MetadataChangeProposalWrapper(
                        entityUrn="urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a",
                        aspect=models.ContainerPropertiesClass(
                            name="test",
                        ),
                    ),
                    models.MetadataChangeEventClass(
                        proposedSnapshot=models.DatasetSnapshotClass(
                            urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,bigquery-public-data.covid19_aha.staffing,PROD)",
                            aspects=[
                                models.DatasetPropertiesClass(
                                    customProperties={
                                        "key": "value",
                                    },
                                ),
                            ],
                        ),
                    ),
                ]
            )
        ),
    ]
    assert list(auto_lowercase_urns(mcws)) == expected


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_container_over_legacy_browse_path(telemetry_ping_mock):
    structure = {"a": {"b": ["c"]}}
    wus = list(
        auto_status_aspect(
            _create_container_aspects(
                structure,
                other_aspects={"b": [models.BrowsePathsClass(paths=["/one/two"])]},
            ),
        )
    )
    new_wus = list(auto_browse_path_v2(wus))
    assert not telemetry_ping_mock.call_count, telemetry_ping_mock.call_args_list
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 3
    )

    paths = _get_browse_paths_from_wu(new_wus)
    assert paths["a"] == []
    assert paths["b"] == _make_container_browse_path_entries(["a"])
    assert paths["c"] == _make_container_browse_path_entries(["a", "b"])


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_with_platform_instance(telemetry_ping_mock):
    platform = "my_platform"
    platform_instance = "my_instance"
    platform_instance_urn = make_dataplatform_instance_urn(platform, platform_instance)
    platform_instance_entry = models.BrowsePathEntryClass(
        platform_instance_urn, platform_instance_urn
    )

    structure = {"a": {"b": ["c"]}}
    wus = list(auto_status_aspect(_create_container_aspects(structure)))

    new_wus = list(
        auto_browse_path_v2(
            wus,
            platform=platform,
            platform_instance=platform_instance,
        )
    )
    assert telemetry_ping_mock.call_count == 0

    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 3
    )
    paths = _get_browse_paths_from_wu(new_wus)
    assert paths["a"] == [platform_instance_entry]
    assert paths["b"] == [
        platform_instance_entry,
        *_make_container_browse_path_entries(["a"]),
    ]
    assert paths["c"] == [
        platform_instance_entry,
        *_make_container_browse_path_entries(["a", "b"]),
    ]


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_invalid_batch_telemetry(telemetry_ping_mock):
    structure = {"a": {"b": ["c"]}}
    b_urn = make_container_urn("b")
    wus = [
        *_create_container_aspects(structure),
        MetadataChangeProposalWrapper(  # Browse path for b separate from its Container aspect
            entityUrn=b_urn,
            aspect=models.BrowsePathsClass(paths=["/one/two"]),
        ).as_workunit(),
    ]
    wus = list(auto_status_aspect(wus))

    assert telemetry_ping_mock.call_count == 0
    _ = list(auto_browse_path_v2(wus))
    assert telemetry_ping_mock.call_count == 1
    assert telemetry_ping_mock.call_args_list[0][0][0] == "incorrect_browse_path_v2"
    assert telemetry_ping_mock.call_args_list[0][0][1]["num_out_of_order"] == 0
    assert telemetry_ping_mock.call_args_list[0][0][1]["num_out_of_batch"] == 1


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_no_invalid_batch_telemetry_for_unrelated_aspects(
    telemetry_ping_mock,
):
    structure = {"a": {"b": ["c"]}}
    b_urn = make_container_urn("b")
    wus = [
        *_create_container_aspects(structure),
        MetadataChangeProposalWrapper(  # Browse path for b separate from its Container aspect
            entityUrn=b_urn,
            aspect=models.ContainerPropertiesClass("container name"),
        ).as_workunit(),
    ]
    wus = list(auto_status_aspect(wus))

    assert telemetry_ping_mock.call_count == 0
    _ = list(auto_browse_path_v2(wus))
    assert telemetry_ping_mock.call_count == 0


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_invalid_order_telemetry(telemetry_ping_mock):
    structure = {"a": {"b": ["c"]}}
    wus = list(reversed(list(_create_container_aspects(structure))))
    wus = list(auto_status_aspect(wus))

    assert telemetry_ping_mock.call_count == 0
    new_wus = list(auto_browse_path_v2(wus))
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        > 0
    )
    assert telemetry_ping_mock.call_count == 1
    assert telemetry_ping_mock.call_args_list[0][0][0] == "incorrect_browse_path_v2"
    assert telemetry_ping_mock.call_args_list[0][0][1]["num_out_of_order"] == 1
    assert telemetry_ping_mock.call_args_list[0][0][1]["num_out_of_batch"] == 0


@patch("datahub.ingestion.api.source_helpers.telemetry.telemetry_instance.ping")
def test_auto_browse_path_v2_dry_run(telemetry_ping_mock):
    structure = {"a": {"b": ["c"]}}
    wus = list(reversed(list(_create_container_aspects(structure))))
    wus = list(auto_status_aspect(wus))

    assert telemetry_ping_mock.call_count == 0
    new_wus = list(auto_browse_path_v2(wus, dry_run=True))
    assert wus == new_wus
    assert (
        sum(bool(wu.get_aspect_of_type(models.BrowsePathsV2Class)) for wu in new_wus)
        == 0
    )
    assert telemetry_ping_mock.call_count == 1


@freeze_time("2023-01-02 00:00:00")
def test_auto_empty_dataset_usage_statistics(caplog: pytest.LogCaptureFixture) -> None:
    has_urn = make_dataset_urn("my_platform", "has_aspect")
    empty_urn = make_dataset_urn("my_platform", "no_aspect")
    config = BaseTimeWindowConfig()
    wus = [
        MetadataChangeProposalWrapper(
            entityUrn=has_urn,
            aspect=models.DatasetUsageStatisticsClass(
                timestampMillis=int(config.start_time.timestamp() * 1000),
                eventGranularity=models.TimeWindowSizeClass(
                    models.CalendarIntervalClass.DAY
                ),
                uniqueUserCount=1,
                totalSqlQueries=1,
            ),
        ).as_workunit()
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        new_wus = list(
            auto_empty_dataset_usage_statistics(
                wus,
                dataset_urns={has_urn, empty_urn},
                config=config,
                all_buckets=False,
            )
        )
        assert not caplog.records

    assert new_wus == [
        *wus,
        MetadataChangeProposalWrapper(
            entityUrn=empty_urn,
            aspect=models.DatasetUsageStatisticsClass(
                timestampMillis=int(datetime(2023, 1, 1).timestamp() * 1000),
                eventGranularity=models.TimeWindowSizeClass(
                    models.CalendarIntervalClass.DAY
                ),
                uniqueUserCount=0,
                totalSqlQueries=0,
                topSqlQueries=[],
                userCounts=[],
                fieldCounts=[],
            ),
        ).as_workunit(),
    ]


@freeze_time("2023-01-02 00:00:00")
def test_auto_empty_dataset_usage_statistics_invalid_timestamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    urn = make_dataset_urn("my_platform", "my_dataset")
    config = BaseTimeWindowConfig()
    wus = [
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=models.DatasetUsageStatisticsClass(
                timestampMillis=0,
                eventGranularity=models.TimeWindowSizeClass(
                    models.CalendarIntervalClass.DAY
                ),
                uniqueUserCount=1,
                totalSqlQueries=1,
            ),
        ).as_workunit()
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        new_wus = list(
            auto_empty_dataset_usage_statistics(
                wus,
                dataset_urns={urn},
                config=config,
                all_buckets=True,
            )
        )
        assert len(caplog.records) == 1
        assert "1970-01-01 00:00:00+00:00" in caplog.records[0].msg

    assert new_wus == [
        *wus,
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=models.DatasetUsageStatisticsClass(
                timestampMillis=int(config.start_time.timestamp() * 1000),
                eventGranularity=models.TimeWindowSizeClass(
                    models.CalendarIntervalClass.DAY
                ),
                uniqueUserCount=0,
                totalSqlQueries=0,
                topSqlQueries=[],
                userCounts=[],
                fieldCounts=[],
            ),
            changeType=models.ChangeTypeClass.CREATE,
        ).as_workunit(),
    ]


def get_sample_mcps(mcps_to_append: List = []) -> List[MetadataChangeProposalWrapper]:
    mcps = [
        MetadataChangeProposalWrapper(
            entityUrn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)",
            aspect=models.OperationClass(
                timestampMillis=10,
                lastUpdatedTimestamp=12,
                operationType=OperationTypeClass.CREATE,
            ),
        ),
        MetadataChangeProposalWrapper(
            entityUrn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)",
            aspect=models.OperationClass(
                timestampMillis=11,
                lastUpdatedTimestamp=20,
                operationType=OperationTypeClass.CREATE,
            ),
        ),
    ]
    mcps.extend(mcps_to_append)
    return mcps


def to_patch_work_units(patch_builder: DatasetPatchBuilder) -> List[MetadataWorkUnit]:
    return [
        MetadataWorkUnit(
            id=MetadataWorkUnit.generate_workunit_id(patch_mcp), mcp_raw=patch_mcp
        )
        for patch_mcp in patch_builder.build()
    ]


def get_auto_generated_wu() -> List[MetadataWorkUnit]:
    dataset_patch_builder = DatasetPatchBuilder(
        urn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)"
    ).set_last_modified(TimeStampClass(time=20))

    auto_generated_work_units = to_patch_work_units(dataset_patch_builder)

    return auto_generated_work_units


@freeze_time("2023-01-02 00:00:00")
def test_auto_patch_last_modified_no_change():
    mcps = [
        MetadataChangeProposalWrapper(
            entityUrn="urn:li:container:008e111aa1d250dd52e0fd5d4b307b1a",
            aspect=models.StatusClass(removed=False),
        )
    ]

    initial_wu = list(auto_workunit(mcps))

    expected = initial_wu

    assert (
        list(auto_patch_last_modified(initial_wu)) == expected
    )  # There should be no change


@freeze_time("2023-01-02 00:00:00")
def test_auto_patch_last_modified_max_last_updated_timestamp():
    mcps = get_sample_mcps()

    expected = list(auto_workunit(mcps))

    auto_generated_work_units = get_auto_generated_wu()

    expected.extend(auto_generated_work_units)

    # work unit should contain a path of datasetProperties with lastModified set to max of operation.lastUpdatedTime
    # i.e., 20
    assert list(auto_patch_last_modified(auto_workunit(mcps))) == expected


@freeze_time("2023-01-02 00:00:00")
def test_auto_patch_last_modified_multi_patch():
    mcps = get_sample_mcps()

    dataset_patch_builder = DatasetPatchBuilder(
        urn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)"
    )

    dataset_patch_builder.set_display_name("foo")
    dataset_patch_builder.set_description("it is fake")

    patch_work_units = to_patch_work_units(dataset_patch_builder)

    work_units = [*list(auto_workunit(mcps)), *patch_work_units]

    auto_generated_work_units = get_auto_generated_wu()

    expected = [*work_units, *auto_generated_work_units]

    # In this case, the final work units include two patch units: one originating from the source and
    # the other from auto_patch_last_modified.
    assert list(auto_patch_last_modified(work_units)) == expected


@freeze_time("2023-01-02 00:00:00")
def test_auto_patch_last_modified_last_modified_patch_exist():
    mcps = get_sample_mcps()

    patch_builder = create_dataset_props_patch_builder(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)",
        dataset_properties=DatasetPropertiesClass(
            name="foo",
            description="dataset for collection of foo",
            lastModified=TimeStampClass(time=20),
        ),
    )

    work_units = [
        *list(auto_workunit(mcps)),
        *to_patch_work_units(patch_builder),
    ]
    # The input and output should align since the source is generating a patch for datasetProperties with the
    # lastModified attribute.
    # Therefore, `auto_patch_last_modified` should not create any additional patch.
    assert list(auto_patch_last_modified(work_units)) == work_units


@freeze_time("2023-01-02 00:00:00")
def test_auto_patch_last_modified_last_modified_patch_not_exist():
    mcps = get_sample_mcps()

    patch_builder = create_dataset_props_patch_builder(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:dbt,abc.foo.bar,PROD)",
        dataset_properties=DatasetPropertiesClass(
            name="foo",
            description="dataset for collection of foo",
        ),
    )

    work_units = [
        *list(auto_workunit(mcps)),
        *to_patch_work_units(patch_builder),
    ]

    expected = [
        *work_units,
        *get_auto_generated_wu(),  # The output should include an additional patch for the `lastModified` attribute.
    ]

    assert list(auto_patch_last_modified(work_units)) == expected
