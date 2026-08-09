"""Renderer-neutral sparse weekly layout and activity summaries."""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from workfold.models import ClassifiedMarker, RecordKind, Source, Weekday

MINUTES_PER_DAY = 24 * 60
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND
NANOSECONDS_PER_DAY = 24 * 60 * NANOSECONDS_PER_MINUTE
_CountKey = TypeVar("_CountKey", Source, RecordKind)


@dataclass(frozen=True, slots=True)
class ClusterCell:
    """The exact, ordered events for one weekday in a time cluster."""

    weekday: Weekday
    markers: tuple[ClassifiedMarker, ...]

    def __post_init__(self) -> None:
        if not self.markers:
            raise ValueError("a cluster cell must contain at least one marker")
        if any(marker.weekday is not self.weekday for marker in self.markers):
            raise ValueError("cluster cell markers must match its weekday")
        if len({marker.marker.marker_id for marker in self.markers}) != len(self.markers):
            raise ValueError("a cluster cell cannot contain duplicate markers")
        if self.markers != tuple(sorted(self.markers, key=_marker_order_key)):
            raise ValueError("cluster cell markers must use deterministic event order")

    @property
    def event_count(self) -> int:
        """Return the number of individually renderable events in this cell."""

        return len(self.markers)


@dataclass(frozen=True, slots=True)
class TimeCluster:
    """One globally aligned, greedily anchored occupied wall-clock row.

    ``start_time_ns`` and ``end_time_ns`` are the first and last observed
    local times, not an estimate of activity duration.  The clustering window
    itself is stored once on :class:`Aggregation`.
    """

    start_time_ns: int
    end_time_ns: int
    cells: tuple[ClusterCell, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.start_time_ns <= self.end_time_ns < NANOSECONDS_PER_DAY:
            raise ValueError("cluster times must form a non-empty range within one day")
        if not self.cells:
            raise ValueError("a time cluster must contain at least one occupied cell")
        weekdays = tuple(cell.weekday for cell in self.cells)
        if weekdays != tuple(sorted(weekdays)) or len(set(weekdays)) != len(weekdays):
            raise ValueError("cluster cells must be unique and ordered by weekday")
        markers = tuple(marker for cell in self.cells for marker in cell.markers)
        observed_times = tuple(marker.time_of_day_ns for marker in markers)
        if min(observed_times) != self.start_time_ns or max(observed_times) != self.end_time_ns:
            raise ValueError("cluster bounds must match its observed event times")

    @property
    def event_count(self) -> int:
        """Return the number of individually renderable events in this row."""

        return sum(cell.event_count for cell in self.cells)

    def cell(self, weekday: Weekday) -> ClusterCell | None:
        """Return the occupied cell for *weekday*, if one exists."""

        return next((cell for cell in self.cells if cell.weekday is weekday), None)


@dataclass(frozen=True, slots=True)
class HiddenMarkers:
    """Markers hidden on one side of an explicit display crop."""

    total: int = 0
    source_counts: tuple[tuple[Source, int], ...] = ()

    def count_for_source(self, source: Source) -> int:
        """Return the hidden marker count for *source*."""

        return _lookup_count(self.source_counts, source)


@dataclass(frozen=True, slots=True)
class Aggregation:
    """Sparse occupied rows plus exact full-scope summary state."""

    cluster_window: timedelta
    display_start_minute: int
    display_end_minute: int
    display_is_explicit: bool
    clusters: tuple[TimeCluster, ...]
    event_count: int
    within_schedule_count: int
    outside_schedule_count: int
    weekend_count: int
    source_counts: tuple[tuple[Source, int], ...]
    record_kind_counts: tuple[tuple[RecordKind, int], ...]
    hidden_before: HiddenMarkers
    hidden_after: HiddenMarkers
    retained_outside_markers: tuple[ClassifiedMarker, ...]
    outside_marker_count: int

    @property
    def outside_omitted_count(self) -> int:
        """Return outside markers not retained because of the list limit."""

        return self.outside_marker_count - len(self.retained_outside_markers)

    @property
    def displayed_event_count(self) -> int:
        """Return the count represented by the sparse chart rows."""

        return sum(cluster.event_count for cluster in self.clusters)

    def count_for_source(self, source: Source) -> int:
        """Return the total marker count for *source*."""

        return _lookup_count(self.source_counts, source)

    def count_for_record_kind(self, record_kind: RecordKind) -> int:
        """Return the total marker count for *record_kind*."""

        return _lookup_count(self.record_kind_counts, record_kind)


def aggregate_markers(
    markers: Iterable[ClassifiedMarker],
    *,
    cluster_window: timedelta,
    schedule_bounds: tuple[int, int] | None = None,
    display_range: tuple[int, int] | None = None,
    outside_limit: int = 50,
) -> Aggregation:
    """Summarize markers and build globally aligned sparse time clusters.

    Visible events are sorted by exact localized time of day.  Each cluster is
    anchored at the earliest unassigned event and contains the half-open range
    ``[anchor, anchor + cluster_window)``.  This intentionally prevents
    transitive chaining from making a row wider than the requested duration.

    ``display_range`` remains a half-open wall-clock minute range.  Cropping is
    applied before clustering, so hidden events cannot affect visible rows.
    All summary counts continue to describe the complete marker input.
    """

    cluster_window_ns = _cluster_window_ns(cluster_window)
    _validate_schedule_bounds(schedule_bounds)
    _validate_display_range(display_range)
    if outside_limit < 0:
        raise ValueError("outside_limit must not be negative")

    visible_markers: list[ClassifiedMarker] = []
    source_counts: Counter[Source] = Counter()
    record_kind_counts: Counter[RecordKind] = Counter()
    hidden_before_sources: Counter[Source] = Counter()
    hidden_after_sources: Counter[Source] = Counter()
    hidden_before_total = 0
    hidden_after_total = 0
    event_count = 0
    within_schedule_count = 0
    outside_schedule_count = 0
    weekend_count = 0
    outside_marker_count = 0
    outside_heap: list[tuple[tuple[int, str], int, ClassifiedMarker]] = []
    occupied_start_ns: int | None = None
    occupied_end_ns: int | None = None

    display_start_ns = display_range[0] * NANOSECONDS_PER_MINUTE if display_range is not None else None
    display_end_ns = display_range[1] * NANOSECONDS_PER_MINUTE if display_range is not None else None

    for classified in markers:
        _validate_classified_marker(classified)
        source, record_kind = _marker_dimensions(classified)
        time_of_day_ns = classified.time_of_day_ns

        event_count += 1
        source_counts[source] += 1
        record_kind_counts[record_kind] += 1
        occupied_start_ns = time_of_day_ns if occupied_start_ns is None else min(occupied_start_ns, time_of_day_ns)
        occupied_end_ns = time_of_day_ns if occupied_end_ns is None else max(occupied_end_ns, time_of_day_ns)

        if classified.within_schedule:
            within_schedule_count += 1
        else:
            outside_schedule_count += 1
            outside_marker_count += 1
            _retain_recent(outside_heap, classified, outside_limit, event_count)
        if classified.weekend:
            weekend_count += 1

        if display_start_ns is None or display_end_ns is None:
            visible_markers.append(classified)
        elif display_start_ns <= time_of_day_ns < display_end_ns:
            visible_markers.append(classified)
        elif time_of_day_ns < display_start_ns:
            hidden_before_total += 1
            hidden_before_sources[source] += 1
        else:
            hidden_after_total += 1
            hidden_after_sources[source] += 1

    display_start, display_end = _resolve_display_range(
        display_range=display_range,
        schedule_bounds=schedule_bounds,
        occupied_start_ns=occupied_start_ns,
        occupied_end_ns=occupied_end_ns,
    )
    clusters = _cluster_markers(visible_markers, cluster_window_ns)
    retained_outside = tuple(item[2] for item in sorted(outside_heap, key=lambda item: (item[0], item[1])))

    aggregation = Aggregation(
        cluster_window=cluster_window,
        display_start_minute=display_start,
        display_end_minute=display_end,
        display_is_explicit=display_range is not None,
        clusters=clusters,
        event_count=event_count,
        within_schedule_count=within_schedule_count,
        outside_schedule_count=outside_schedule_count,
        weekend_count=weekend_count,
        source_counts=_freeze_counter(source_counts),
        record_kind_counts=_freeze_counter(record_kind_counts),
        hidden_before=HiddenMarkers(hidden_before_total, _freeze_counter(hidden_before_sources)),
        hidden_after=HiddenMarkers(hidden_after_total, _freeze_counter(hidden_after_sources)),
        retained_outside_markers=retained_outside,
        outside_marker_count=outside_marker_count,
    )
    if aggregation.displayed_event_count + hidden_before_total + hidden_after_total != event_count:
        raise RuntimeError("displayed and hidden marker totals do not reconcile")
    return aggregation


def _cluster_markers(markers: Iterable[ClassifiedMarker], window_ns: int) -> tuple[TimeCluster, ...]:
    ordered = sorted(markers, key=_marker_order_key)
    clusters: list[TimeCluster] = []
    cursor = 0
    while cursor < len(ordered):
        anchor = ordered[cursor].time_of_day_ns
        window_end = anchor + window_ns
        end = cursor + 1
        while end < len(ordered) and ordered[end].time_of_day_ns < window_end:
            end += 1
        cluster_markers = ordered[cursor:end]
        by_weekday: dict[Weekday, list[ClassifiedMarker]] = {}
        for marker in cluster_markers:
            by_weekday.setdefault(marker.weekday, []).append(marker)
        cells = tuple(ClusterCell(weekday, tuple(by_weekday[weekday])) for weekday in sorted(by_weekday))
        clusters.append(
            TimeCluster(
                start_time_ns=anchor,
                end_time_ns=cluster_markers[-1].time_of_day_ns,
                cells=cells,
            )
        )
        cursor = end
    return tuple(clusters)


def _marker_order_key(classified: ClassifiedMarker) -> tuple[int, int, int, str]:
    """Order events by wall time, instant, source, then provenance.

    Source is only consulted for genuinely simultaneous folded events, where
    no chronological ordering exists.  Keeping Git before filesystem makes
    mixed cells visually stable instead of exposing hash-derived marker IDs.
    """

    return (
        classified.time_of_day_ns,
        classified.marker.occurred_at_utc_ns,
        0 if classified.marker.origin.source is Source.GIT else 1,
        classified.marker.marker_id,
    )


def _marker_dimensions(classified: ClassifiedMarker) -> tuple[Source, RecordKind]:
    observations = classified.marker.observations
    if not observations:
        raise ValueError("an activity marker must contain at least one observation")
    source = observations[0].origin.source
    record_kind = observations[0].origin.record_kind
    for observation in observations[1:]:
        if observation.origin.source is not source or observation.origin.record_kind is not record_kind:
            raise ValueError("coalesced marker observations must share source and record kind")
    return source, record_kind


def _validate_classified_marker(classified: ClassifiedMarker) -> None:
    if not 0 <= classified.time_of_day_ns < NANOSECONDS_PER_DAY:
        raise ValueError("classified marker local time must fall within one day")


def _cluster_window_ns(cluster_window: object) -> int:
    if not isinstance(cluster_window, timedelta):
        raise TypeError("cluster_window must be a datetime.timedelta")
    nanoseconds = (
        cluster_window.days * 86_400 * NANOSECONDS_PER_SECOND
        + cluster_window.seconds * NANOSECONDS_PER_SECOND
        + cluster_window.microseconds * 1_000
    )
    if not 0 < nanoseconds < NANOSECONDS_PER_DAY:
        raise ValueError("cluster_window must be greater than zero and less than 24 hours")
    return nanoseconds


def _retain_recent(
    heap: list[tuple[tuple[int, str], int, ClassifiedMarker]],
    classified: ClassifiedMarker,
    limit: int,
    ordinal: int,
) -> None:
    if limit == 0:
        return
    key = (classified.marker.occurred_at_utc_ns, classified.marker.marker_id)
    item = (key, ordinal, classified)
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif key > heap[0][0]:
        heapq.heapreplace(heap, item)


def _resolve_display_range(
    *,
    display_range: tuple[int, int] | None,
    schedule_bounds: tuple[int, int] | None,
    occupied_start_ns: int | None,
    occupied_end_ns: int | None,
) -> tuple[int, int]:
    if display_range is not None:
        return display_range

    starts: list[int] = []
    ends: list[int] = []
    if schedule_bounds is not None:
        starts.append(schedule_bounds[0])
        ends.append(schedule_bounds[1])
    if occupied_start_ns is not None and occupied_end_ns is not None:
        starts.append(occupied_start_ns // NANOSECONDS_PER_MINUTE)
        ends.append(occupied_end_ns // NANOSECONDS_PER_MINUTE + 1)
    if not starts:
        return (0, MINUTES_PER_DAY)

    start = max(0, (min(starts) // 60) * 60)
    end = min(MINUTES_PER_DAY, ((max(ends) + 59) // 60) * 60)
    if start == end:
        end = min(MINUTES_PER_DAY, start + 60)
        if start == end:
            start = max(0, end - 60)
    return (start, end)


def _validate_schedule_bounds(bounds: tuple[int, int] | None) -> None:
    if bounds is None:
        return
    start, end = bounds
    if not 0 <= start < end <= MINUTES_PER_DAY:
        raise ValueError("schedule_bounds must be within 00:00-24:00 and non-empty")


def _validate_display_range(display_range: tuple[int, int] | None) -> None:
    if display_range is None:
        return
    start, end = display_range
    if not 0 <= start < end <= MINUTES_PER_DAY:
        raise ValueError("display_range must be within 00:00-24:00 and non-empty")


def _freeze_counter(counter: Counter[_CountKey]) -> tuple[tuple[_CountKey, int], ...]:
    return tuple(sorted(((key, count) for key, count in counter.items() if count), key=lambda item: item[0].value))


def _lookup_count(pairs: tuple[tuple[_CountKey, int], ...], key: _CountKey) -> int:
    return next((count for candidate, count in pairs if candidate is key), 0)


__all__ = [
    "Aggregation",
    "ClusterCell",
    "HiddenMarkers",
    "TimeCluster",
    "aggregate_markers",
]
