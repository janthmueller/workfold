from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workfold.aggregation import NANOSECONDS_PER_MINUTE, aggregate_markers
from workfold.models import (
    ActivityMarker,
    ClassifiedMarker,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    Weekday,
)
from workfold.time_ranges import datetime_to_utc_ns


def _classified(
    identifier: str,
    local_datetime: datetime,
    *,
    source: Source = Source.GIT,
    within_schedule: bool = True,
    submicrosecond_ns: int = 0,
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
    observation = TimestampObservation.create(origin, timestamp_kind, instant_ns, str(instant_ns))
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
    assert monday_cell.markers == tuple(markers[:2])
    assert monday_cell.event_count == 2
    assert result.event_count == result.displayed_event_count == 3
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
    assert first.start_time_ns == 8 * 60 * NANOSECONDS_PER_MINUTE
    assert first.cell(Weekday.MONDAY) is not None
    assert first.cell(Weekday.TUESDAY) is not None
    assert first.cell(Weekday.WEDNESDAY) is None
    assert second.cell(Weekday.WEDNESDAY) is not None


def test_anchored_clustering_does_not_chain_neighboring_events() -> None:
    base = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    markers = [
        _classified("zero", base),
        _classified("nine", base.replace(minute=9)),
        _classified("eighteen", base.replace(minute=18)),
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(minutes=10))

    assert [cluster.event_count for cluster in result.clusters] == [2, 1]
    assert result.clusters[1].start_time_ns == (9 * 60 + 18) * NANOSECONDS_PER_MINUTE


def test_cluster_window_supports_sub_minute_precision() -> None:
    base = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    markers = [
        _classified("anchor", base),
        _classified("inside", base.replace(second=30, microsecond=499999), submicrosecond_ns=999),
        _classified("boundary", base.replace(second=30, microsecond=500000)),
    ]

    result = aggregate_markers(markers, cluster_window=timedelta(seconds=30, microseconds=500_000))

    assert [cluster.event_count for cluster in result.clusters] == [2, 1]


def test_event_order_is_exact_and_independent_of_input_order() -> None:
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
        cluster_window=timedelta(seconds=1),
    )
    reverse = aggregate_markers(
        (earlier_week, exact_later, later_week),
        cluster_window=timedelta(seconds=1),
    )

    forward_markers = forward.clusters[0].cell(Weekday.MONDAY)
    reverse_markers = reverse.clusters[0].cell(Weekday.MONDAY)
    assert forward_markers is not None and reverse_markers is not None
    assert forward_markers.markers == reverse_markers.markers == (earlier_week, later_week, exact_later)
    assert exact_later.time_of_day_ns == earlier_week.time_of_day_ns + 1


def test_simultaneous_mixed_sources_have_a_stable_git_first_tie_break() -> None:
    instant = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    filesystem = _classified("filesystem", instant, source=Source.FILESYSTEM)
    git = _classified("git", instant, source=Source.GIT)

    result = aggregate_markers((filesystem, git), cluster_window=timedelta(minutes=10))

    cell = result.clusters[0].cell(Weekday.MONDAY)
    assert cell is not None
    assert cell.markers == (git, filesystem)


def test_fall_back_duplicate_wall_times_remain_two_events_in_one_cell() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    first = _classified("first-fold", datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0))
    second = _classified("second-fold", datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1))

    result = aggregate_markers((second, first), cluster_window=timedelta(minutes=10))

    cell = result.clusters[0].cell(Weekday.SUNDAY)
    assert cell is not None
    assert cell.markers == (first, second)
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
    assert [cell.markers for cluster in result.clusters for cell in cluster.cells] == [(markers[1],), (markers[2],)]


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
    assert result.clusters[0].start_time_ns == 8 * 60 * NANOSECONDS_PER_MINUTE
    assert result.clusters[0].event_count == 2


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
    [timedelta(0), timedelta(microseconds=-1), timedelta(days=1), timedelta(days=2)],
)
def test_cluster_window_must_be_positive_and_shorter_than_a_day(cluster_window: timedelta) -> None:
    with pytest.raises(ValueError, match="cluster_window"):
        aggregate_markers((), cluster_window=cluster_window)


def test_cluster_window_requires_timedelta() -> None:
    with pytest.raises(TypeError, match="timedelta"):
        aggregate_markers((), cluster_window=600)  # type: ignore[arg-type]


def test_aggregation_validates_other_public_options() -> None:
    with pytest.raises(ValueError, match="schedule_bounds"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), schedule_bounds=(500, 400))
    with pytest.raises(ValueError, match="display_range"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), display_range=(0, 1500))
    with pytest.raises(ValueError, match="outside_limit"):
        aggregate_markers((), cluster_window=timedelta(minutes=10), outside_limit=-1)


def test_empty_aggregation_has_no_rows_and_uses_full_day_without_schedule_bounds() -> None:
    result = aggregate_markers((), cluster_window=timedelta(minutes=10), outside_limit=0)

    assert result.event_count == result.displayed_event_count == 0
    assert result.clusters == ()
    assert result.retained_outside_markers == ()
    assert (result.display_start_minute, result.display_end_minute) == (0, 24 * 60)
