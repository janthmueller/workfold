from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workfold.configuration import ClusterAnchor
from workfold.domain.observations import (
    ActivityMarker,
    ClassifiedMarker,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    Weekday,
)
from workfold.domain.time import datetime_to_utc_ns
from workfold.folding import (
    NANOSECONDS_PER_MINUTE,
    AggregationBuilder,
    ClusterCell,
    MarkerRun,
    TimeCluster,
    aggregate_markers,
)
from workfold.folding.spill import ChartMarkerStore


def _classified(
    identifier: str,
    local_datetime: datetime,
    *,
    source: Source = Source.GIT,
    within_schedule: bool = True,
    submicrosecond_ns: int = 0,
    actor_name: str = "Fixture",
    actor_email: str = "fixture@example.test",
) -> ClassifiedMarker:
    if not 0 <= submicrosecond_ns < 1_000:
        raise ValueError("test remainder must be sub-microsecond")
    if source is Source.GIT:
        record_kind = RecordKind.COMMIT
        timestamp_kind = TimestampKind.GIT_AUTHOR
    else:
        record_kind = RecordKind.FILESYSTEM_ENTRY
        timestamp_kind = TimestampKind.FS_MODIFIED
    origin = RecordOrigin(
        record_id=f"record-{identifier}",
        source=source,
        record_kind=record_kind,
        repository_or_root=Path("/work"),
        commit_id=identifier if source is Source.GIT else None,
        path=Path(identifier) if source is Source.FILESYSTEM else None,
    )
    instant_ns = datetime_to_utc_ns(local_datetime) + submicrosecond_ns
    observation = TimestampObservation.create(
        origin,
        timestamp_kind,
        instant_ns,
        str(instant_ns),
        original_offset_minutes=0 if source is Source.GIT else None,
        actor_name=actor_name if source is Source.GIT else None,
        actor_email=actor_email if source is Source.GIT else None,
    )
    marker = ActivityMarker.create((observation,))
    return ClassifiedMarker(marker=marker, local_datetime=local_datetime, within_schedule=within_schedule)


def test_sparse_aggregation_preserves_exact_events_and_summary_dimensions() -> None:
    monday = datetime(2026, 8, 3, 8, 5, tzinfo=timezone.utc)
    markers = [
        _classified("a", monday, within_schedule=True),
        _classified("b", monday.replace(minute=9), source=Source.FILESYSTEM, within_schedule=False),
        _classified("c", datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc), within_schedule=False),
    ]

    result = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        schedule_bounds=(8 * 60, 16 * 60 + 30),
    )

    assert len(result.clusters) == 2
    monday_cell = result.clusters[0].cell(Weekday.MONDAY)
    assert monday_cell is not None
    assert monday_cell.runs == (
        MarkerRun(Source.GIT, True, 1),
        MarkerRun(Source.FILESYSTEM, False, 1),
    )
    assert monday_cell.event_count == 2
    assert result.event_count == result.displayed_event_count == 3
    assert result.visible_weekdays == tuple(Weekday)
    assert result.hidden_weekday_counts == ()
    assert result.within_schedule_count == 1
    assert result.outside_schedule_count == 2
    assert result.weekend_count == 1
    assert result.count_for_source(Source.GIT) == 2
    assert result.count_for_source(Source.FILESYSTEM) == 1
    assert result.count_for_record_kind(RecordKind.COMMIT) == 2
    assert result.count_for_record_kind(RecordKind.FILESYSTEM_ENTRY) == 1
    assert (result.display_start_minute, result.display_end_minute) == (8 * 60, 17 * 60)


def test_clusters_are_global_across_weekdays_and_half_open_at_the_anchor_window() -> None:
    markers = [
        _classified("monday", datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)),
        _classified("tuesday", datetime(2026, 8, 4, 8, 9, 59, 999999, tzinfo=timezone.utc), submicrosecond_ns=999),
        _classified("wednesday", datetime(2026, 8, 5, 8, 10, tzinfo=timezone.utc)),
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(minutes=10))

    assert len(result.clusters) == 2
    first, second = result.clusters
    assert first.band_start_time_ns == 8 * 60 * NANOSECONDS_PER_MINUTE
    assert first.start_time_ns == first.observed_start_time_ns
    assert first.end_time_ns == first.observed_end_time_ns
    assert first.cell(Weekday.MONDAY) is not None
    assert first.cell(Weekday.TUESDAY) is not None
    assert first.cell(Weekday.WEDNESDAY) is None
    assert second.cell(Weekday.WEDNESDAY) is not None


def test_time_cluster_preserves_the_exported_observed_bounds_constructor() -> None:
    cell = ClusterCell(Weekday.MONDAY, (MarkerRun(Source.GIT, True, 1),))

    cluster = TimeCluster(100, 200, (cell,))

    assert cluster.observed_start_time_ns == 100
    assert cluster.observed_end_time_ns == 200
    assert cluster.band_start_time_ns == 100
    assert cluster.band_end_time_ns == 201


def test_anchored_clustering_does_not_chain_neighboring_events() -> None:
    base = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    markers = [
        _classified("zero", base),
        _classified("nine", base.replace(minute=9)),
        _classified("eighteen", base.replace(minute=18)),
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(minutes=10))

    assert [cluster.event_count for cluster in result.clusters] == [2, 1]
    assert result.clusters[1].band_start_time_ns == (9 * 60 + 18) * NANOSECONDS_PER_MINUTE


def test_midnight_clustering_uses_fixed_half_open_clock_intervals() -> None:
    markers = [
        _classified("first", datetime(2026, 8, 3, 8, 40, tzinfo=timezone.utc)),
        _classified("inside-event-window", datetime(2026, 8, 4, 9, 19, 59, 999999, tzinfo=timezone.utc)),
        _classified("clock-boundary", datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)),
    ]

    event_anchored = aggregate_markers(markers, cluster_window=timedelta(hours=1))
    midnight_anchored = aggregate_markers(
        markers,
        cluster_window=timedelta(hours=1),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
    )

    assert [cluster.event_count for cluster in event_anchored.clusters] == [3]
    assert [cluster.event_count for cluster in midnight_anchored.clusters] == [1, 2]
    first, second = midnight_anchored.clusters
    assert (first.band_start_time_ns, first.band_end_time_ns) == (
        8 * 60 * NANOSECONDS_PER_MINUTE,
        9 * 60 * NANOSECONDS_PER_MINUTE,
    )
    assert (first.observed_start_time_ns, first.observed_end_time_ns) == (
        (8 * 60 + 40) * NANOSECONDS_PER_MINUTE,
        (8 * 60 + 40) * NANOSECONDS_PER_MINUTE,
    )
    assert (second.band_start_time_ns, second.band_end_time_ns) == (
        9 * 60 * NANOSECONDS_PER_MINUTE,
        10 * 60 * NANOSECONDS_PER_MINUTE,
    )
    assert second.cell(Weekday.TUESDAY) is not None
    assert second.cell(Weekday.WEDNESDAY) is not None


def test_midnight_clustering_clips_a_nondividing_final_interval_at_day_end() -> None:
    marker = _classified("late", datetime(2026, 8, 3, 23, 59, 59, tzinfo=timezone.utc))

    result = aggregate_markers(
        (marker,),
        cluster_window=timedelta(hours=1, minutes=5),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
    )

    cluster = result.clusters[0]
    assert cluster.band_start_time_ns == (23 * 60 + 50) * NANOSECONDS_PER_MINUTE
    assert cluster.band_end_time_ns == 24 * 60 * NANOSECONDS_PER_MINUTE


def test_midnight_clustering_aligns_automatic_display_bounds_to_complete_bands() -> None:
    marker = _classified("event", datetime(2026, 8, 3, 8, 45, tzinfo=timezone.utc))

    result = aggregate_markers(
        (marker,),
        cluster_window=timedelta(hours=1, minutes=5),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        schedule_bounds=(8 * 60, 16 * 60 + 30),
    )

    assert not result.display_is_explicit
    assert (result.display_start_minute, result.display_end_minute) == (7 * 60 + 35, 17 * 60 + 20)


def test_cluster_window_preserves_subsecond_precision_above_one_minute() -> None:
    base = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    markers = [
        _classified("anchor", base),
        _classified("inside", base.replace(minute=1, second=30, microsecond=499999), submicrosecond_ns=999),
        _classified("boundary", base.replace(minute=1, second=30, microsecond=500000)),
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(minutes=1, seconds=30, microseconds=500_000))

    assert [cluster.event_count for cluster in result.clusters] == [2, 1]


def test_visual_runs_are_exact_and_independent_of_input_order() -> None:
    later_week = _classified(
        "later-week",
        datetime(2026, 8, 10, 8, 0, 0, 123456, tzinfo=timezone.utc),
        submicrosecond_ns=900,
    )
    earlier_week = _classified(
        "earlier-week",
        datetime(2026, 8, 3, 8, 0, 0, 123456, tzinfo=timezone.utc),
        submicrosecond_ns=900,
    )
    exact_later = _classified(
        "exact-later",
        datetime(2026, 8, 3, 8, 0, 0, 123456, tzinfo=timezone.utc),
        submicrosecond_ns=901,
    )

    forward = aggregate_markers(
        (later_week, exact_later, earlier_week),
        cluster_window=timedelta(minutes=1),
    )
    reverse = aggregate_markers(
        (earlier_week, exact_later, later_week),
        cluster_window=timedelta(minutes=1),
    )

    forward_markers = forward.clusters[0].cell(Weekday.MONDAY)
    reverse_markers = reverse.clusters[0].cell(Weekday.MONDAY)
    assert forward_markers is not None and reverse_markers is not None
    assert forward_markers.runs == reverse_markers.runs == (MarkerRun(Source.GIT, True, 3),)
    assert exact_later.time_of_day_ns == earlier_week.time_of_day_ns + 1


def test_simultaneous_mixed_sources_have_a_stable_git_first_tie_break() -> None:
    instant = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    filesystem = _classified("filesystem", instant, source=Source.FILESYSTEM)
    git = _classified("git", instant, source=Source.GIT)

    result = aggregate_markers((filesystem, git), cluster_window=timedelta(minutes=10))

    cell = result.clusters[0].cell(Weekday.MONDAY)
    assert cell is not None
    assert cell.runs == (
        MarkerRun(Source.GIT, True, 1),
        MarkerRun(Source.FILESYSTEM, True, 1),
    )


def test_fall_back_duplicate_wall_times_remain_two_events_in_one_cell() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    first = _classified("first-fold", datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0))
    second = _classified("second-fold", datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1))

    result = aggregate_markers((second, first), cluster_window=timedelta(minutes=10))

    cell = result.clusters[0].cell(Weekday.SUNDAY)
    assert cell is not None
    assert cell.runs == (MarkerRun(Source.GIT, True, 2),)
    assert cell.event_count == 2


def test_explicit_display_crop_is_exact_and_does_not_change_full_summary() -> None:
    markers = [
        _classified("early", datetime(2026, 8, 3, 5, 59, 59, 999999, tzinfo=timezone.utc)),
        _classified("start", datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc), source=Source.FILESYSTEM),
        _classified("shown", datetime(2026, 8, 3, 21, 59, 59, 999999, tzinfo=timezone.utc)),
        _classified("end", datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)),
    ]

    result = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        display_range=(6 * 60, 22 * 60),
    )

    assert result.event_count == 4
    assert result.displayed_event_count == 2
    assert result.hidden_before.total == 1
    assert result.hidden_before.count_for_source(Source.GIT) == 1
    assert result.hidden_after.total == 1
    assert result.hidden_after.count_for_source(Source.GIT) == 1
    assert (result.display_start_minute, result.display_end_minute) == (6 * 60, 22 * 60)
    assert [cell.runs for cluster in result.clusters for cell in cluster.cells] == [
        (MarkerRun(Source.FILESYSTEM, True, 1),),
        (MarkerRun(Source.GIT, True, 1),),
    ]


def test_cropping_happens_before_clustering() -> None:
    markers = [
        _classified("hidden-anchor", datetime(2026, 8, 3, 7, 59, tzinfo=timezone.utc)),
        _classified("visible", datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)),
        _classified("visible-nine", datetime(2026, 8, 3, 8, 9, tzinfo=timezone.utc)),
    ]

    result = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        display_range=(8 * 60, 9 * 60),
    )

    assert result.hidden_before.total == 1
    assert len(result.clusters) == 1
    assert result.clusters[0].band_start_time_ns == 8 * 60 * NANOSECONDS_PER_MINUTE
    assert result.clusters[0].event_count == 2


def test_explicitly_hidden_days_keep_totals_but_do_not_anchor_visible_clusters() -> None:
    markers = [
        _classified(
            "hidden-saturday",
            datetime(2026, 8, 8, 7, 59, tzinfo=timezone.utc),
            within_schedule=False,
        ),
        _classified("visible-monday", datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)),
        _classified("visible-monday-two", datetime(2026, 8, 3, 8, 9, tzinfo=timezone.utc)),
    ]

    result = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        hide_days=(Weekday.SATURDAY, Weekday.SUNDAY),
        outside_limit=1,
    )

    assert result.visible_weekdays == tuple(day for day in Weekday if not day.is_weekend)
    assert result.event_count == 3
    assert result.displayed_event_count == 2
    assert result.weekend_count == 1
    assert result.hidden_weekday_counts == ((Weekday.SATURDAY, 1),)
    assert result.hidden_weekday_event_count == 1
    assert [marker.marker.marker_id for marker in result.retained_outside_markers] == [markers[0].marker.marker_id]
    assert len(result.clusters) == 1
    assert result.clusters[0].band_start_time_ns == 8 * 60 * NANOSECONDS_PER_MINUTE


def test_empty_day_hiding_is_conditional_and_composable_by_scope() -> None:
    markers = [
        _classified("monday", datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)),
        _classified("saturday", datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)),
    ]

    weekend_only = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        hide_empty_days=(Weekday.SATURDAY, Weekday.SUNDAY),
    )
    all_empty = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        hide_empty_days=tuple(Weekday),
    )

    assert weekend_only.visible_weekdays == (
        Weekday.MONDAY,
        Weekday.TUESDAY,
        Weekday.WEDNESDAY,
        Weekday.THURSDAY,
        Weekday.FRIDAY,
        Weekday.SATURDAY,
    )
    assert all_empty.visible_weekdays == (Weekday.MONDAY, Weekday.SATURDAY)
    assert weekend_only.hidden_weekday_event_count == all_empty.hidden_weekday_event_count == 0
    assert weekend_only.displayed_event_count == all_empty.displayed_event_count == 2


def test_empty_day_hiding_uses_the_visible_time_crop() -> None:
    result = aggregate_markers(
        (_classified("cropped-tuesday", datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc)),),
        cluster_window=timedelta(minutes=10),
        display_range=(6 * 60, 22 * 60),
        hide_empty_days=tuple(Weekday),
    )

    assert result.visible_weekdays == ()
    assert result.event_count == 1
    assert result.displayed_event_count == 0
    assert result.hidden_before.total == 1
    assert result.hidden_weekday_event_count == 0


def test_hidden_day_identities_are_not_retained_in_the_visible_registry() -> None:
    markers = [
        _classified(
            "visible-ada",
            datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        _classified(
            "hidden-bob",
            datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc),
            actor_name="Bob",
            actor_email="bob@example.test",
        ),
    ]

    result = aggregate_markers(
        markers,
        cluster_window=timedelta(minutes=10),
        retain_git_identities=True,
        hide_days=(Weekday.SATURDAY, Weekday.SUNDAY),
    )

    assert [identity.members[0].name for identity in result.identities] == ["Ada"]
    assert result.identity_counts == ((0, 1),)


def test_outside_list_retains_only_most_recent_markers_in_chronological_order() -> None:
    markers = [
        _classified(
            str(hour),
            datetime(2026, 8, 3, hour, 0, tzinfo=timezone.utc),
            within_schedule=False,
        )
        for hour in (11, 8, 10, 9)
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(minutes=10), outside_limit=2)

    assert result.outside_marker_count == 4
    assert result.outside_omitted_count == 2
    assert [item.local_datetime.hour for item in result.retained_outside_markers] == [10, 11]


@pytest.mark.parametrize(
    "cluster_window",
    [
        timedelta(0),
        timedelta(microseconds=-1),
        timedelta(seconds=59, microseconds=999_999),
        timedelta(days=1),
        timedelta(days=2),
    ],
)
def test_cluster_window_must_be_at_least_one_minute_and_shorter_than_a_day(cluster_window: timedelta) -> None:
    with pytest.raises(ValueError, match="cluster_window"):
        aggregate_markers((), cluster_window=cluster_window)


def test_cluster_window_requires_timedelta() -> None:
    with pytest.raises(TypeError, match="timedelta"):
        aggregate_markers((), cluster_window=600)  # type: ignore[arg-type]


def test_cluster_anchor_requires_the_domain_enum() -> None:
    with pytest.raises(TypeError, match="cluster_anchor"):
        aggregate_markers(
            (),
            cluster_window=timedelta(minutes=10),
            cluster_anchor="midnight",  # type: ignore[arg-type]
        )


def test_aggregation_snapshot_revalidates_the_cluster_anchor() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(TypeError, match="cluster_anchor"):
        replace(result, cluster_anchor="midnight")  # type: ignore[arg-type]


def test_aggregation_snapshot_revalidates_midnight_window_alignment() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(ValueError, match="midnight-anchored cluster_window must use whole minutes"):
        replace(
            result,
            cluster_anchor=ClusterAnchor.MIDNIGHT,
            cluster_window=timedelta(minutes=1, seconds=30),
        )


@pytest.mark.parametrize(
    "cluster_window",
    [timedelta(0), timedelta(seconds=59, microseconds=999_999), timedelta(days=1)],
)
def test_aggregation_snapshot_revalidates_cluster_window_bounds(cluster_window: timedelta) -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(ValueError, match="cluster_window must be at least one minute and less than 24 hours"):
        replace(result, cluster_window=cluster_window)


def test_aggregation_snapshot_revalidates_cluster_window_type() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(TypeError, match="cluster_window must be a datetime.timedelta"):
        replace(result, cluster_window=600)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 60), (60, 60), (61, 60), (0, 1441)],
)
def test_aggregation_snapshot_revalidates_display_bounds(start: int, end: int) -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(ValueError, match="display bounds must be within 00:00-24:00 and non-empty"):
        replace(result, display_start_minute=start, display_end_minute=end)


@pytest.mark.parametrize(
    ("field", "value"),
    [("display_start_minute", 0.0), ("display_end_minute", True)],
)
def test_aggregation_snapshot_revalidates_display_bound_types(field: str, value: object) -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(TypeError, match="display bounds must be integer minutes"):
        replace(result, **{field: value})


def test_aggregation_snapshot_revalidates_explicit_display_flag_type() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10))

    with pytest.raises(TypeError, match="display_is_explicit must be a bool"):
        replace(result, display_is_explicit="yes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "cluster_window",
    [timedelta(minutes=1, seconds=30), timedelta(minutes=1, microseconds=1)],
)
def test_midnight_cluster_anchor_requires_whole_minute_windows(cluster_window: timedelta) -> None:
    with pytest.raises(ValueError, match="midnight-anchored cluster_window must use whole minutes"):
        aggregate_markers(
            (),
            cluster_window=cluster_window,
            cluster_anchor=ClusterAnchor.MIDNIGHT,
        )


def test_aggregation_validates_other_public_options() -> None:
    with pytest.raises(ValueError, match="schedule_bounds"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), schedule_bounds=(500, 400))
    with pytest.raises(ValueError, match="display_range"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), display_range=(0, 1500))
    with pytest.raises(ValueError, match="outside_limit"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), outside_limit=-1)
    with pytest.raises(ValueError, match="cluster_materialization_threshold"):
        AggregationBuilder(
            cluster_window=timedelta(minutes=10),
            cluster_materialization_threshold=-1,
        )
    with pytest.raises(ValueError, match="leave at least one"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), hide_days=tuple(Weekday))
    with pytest.raises(TypeError, match="Weekday values"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), hide_empty_days=(7,))  # type: ignore[arg-type]


def test_empty_aggregation_has_no_rows_and_uses_full_day_without_schedule_bounds() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10), outside_limit=0)

    assert result.event_count == result.displayed_event_count == 0
    assert result.clusters == ()
    assert result.retained_outside_markers == ()
    assert (result.display_start_minute, result.display_end_minute) == (0, 24 * 60)


@pytest.mark.parametrize("cluster_anchor", tuple(ClusterAnchor))
def test_spilled_sort_matches_in_memory_sort_and_cleans_up(cluster_anchor: ClusterAnchor) -> None:
    markers = [
        _classified(
            str(index),
            datetime(1960, 8, 1, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
            source=Source.FILESYSTEM if index % 2 else Source.GIT,
            within_schedule=index % 3 != 0,
        )
        for index in range(8)
    ]
    expected = aggregate_markers(
        reversed(markers),
        cluster_window=timedelta(minutes=5),
        cluster_anchor=cluster_anchor,
    )
    builder = AggregationBuilder(
        cluster_window=timedelta(minutes=5),
        cluster_anchor=cluster_anchor,
        spill_threshold=2,
        cluster_materialization_threshold=0,
    )
    for marker in reversed(markers):
        builder.add(marker)

    actual = builder.build()

    assert builder.did_spill
    assert not isinstance(actual.clusters, tuple)
    assert tuple(actual.clusters) == expected.clusters
    assert actual == expected
    with pytest.raises(RuntimeError, match="only be built once"):
        builder.build()
    with pytest.raises(RuntimeError, match="cannot be reused"):
        builder.add(markers[0])


def test_aggregation_builder_can_be_closed_idempotently_after_an_aborted_run() -> None:
    builder = AggregationBuilder(cluster_window=timedelta(minutes=5), spill_threshold=1)
    builder.add(
        _classified(
            "first",
            datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        )
    )
    builder.add(
        _classified(
            "second",
            datetime(2026, 8, 3, 9, 1, tzinfo=timezone.utc),
        )
    )

    assert builder.did_spill
    builder.close()
    builder.close()

    with pytest.raises(RuntimeError, match="cannot be reused"):
        builder.add(
            _classified(
                "third",
                datetime(2026, 8, 3, 9, 2, tzinfo=timezone.utc),
            )
        )


def test_aggregation_builder_cleans_up_spill_when_snapshot_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = AggregationBuilder(cluster_window=timedelta(minutes=5), spill_threshold=1)
    builder.add(_classified("first", datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)))
    builder.add(_classified("second", datetime(2026, 8, 3, 9, 1, tzinfo=timezone.utc)))
    close_count = 0
    original_close = ChartMarkerStore.close

    def recording_close(store: ChartMarkerStore) -> None:
        nonlocal close_count
        close_count += 1
        original_close(store)

    def fail_resolution(_builder: AggregationBuilder) -> tuple[Weekday, ...]:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(ChartMarkerStore, "close", recording_close)
    monkeypatch.setattr(AggregationBuilder, "_resolve_visible_weekdays", fail_resolution)

    with pytest.raises(RuntimeError, match="snapshot failed"):
        builder.build()

    assert close_count == 1


def test_identity_registry_is_deterministic_and_survives_sqlite_spill() -> None:
    markers = [
        _classified(
            "bob",
            datetime(2026, 8, 3, 9, 1, tzinfo=timezone.utc),
            actor_name="Bob",
            actor_email="bob@example.test",
        ),
        _classified(
            "ada",
            datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        _classified(
            "bob-two",
            datetime(2026, 8, 3, 9, 2, tzinfo=timezone.utc),
            actor_name="Bob",
            actor_email="bob@example.test",
        ),
    ]
    expected = aggregate_markers(
        reversed(markers),
        cluster_window=timedelta(minutes=5),
        retain_git_identities=True,
    )
    builder = AggregationBuilder(
        cluster_window=timedelta(minutes=5),
        retain_git_identities=True,
        spill_threshold=2,
    )
    for marker in markers:
        builder.add(marker)

    actual = builder.build()

    assert builder.did_spill
    assert actual == expected
    assert [identity.members[0].name for identity in actual.identities] == ["Ada", "Bob"]
    assert actual.identity_counts == ((0, 1), (1, 2))


def test_pathologically_alternating_cell_is_compacted_to_bounded_counts() -> None:
    base = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    markers = tuple(
        _classified(
            str(index),
            base + timedelta(microseconds=index),
            source=Source.FILESYSTEM if index % 2 else Source.GIT,
        )
        for index in range(300)
    )

    result = aggregate_markers(markers, cluster_window=timedelta(hours=1))

    cell = result.clusters[0].cell(Weekday.MONDAY)
    assert cell is not None
    assert cell.compacted
    assert cell.event_count == 300
    assert cell.runs == (
        MarkerRun(Source.GIT, True, 150),
        MarkerRun(Source.FILESYSTEM, True, 150),
    )
