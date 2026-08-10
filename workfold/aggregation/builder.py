"""Streaming aggregation orchestration."""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Iterable
from datetime import timedelta

from workfold.aggregation.layout import cluster_ordered_markers
from workfold.aggregation.markers import VISUAL_ORDER, ChartMarker, marker_dimensions
from workfold.aggregation.models import (
    MINUTES_PER_DAY,
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_MINUTE,
    NANOSECONDS_PER_SECOND,
    Aggregation,
    HiddenMarkers,
    freeze_counter,
)
from workfold.aggregation.spill import DEFAULT_SPILL_THRESHOLD, ChartMarkerStore
from workfold.models import ClassifiedMarker, RecordKind, Source

CLUSTER_MATERIALIZATION_THRESHOLD = 4_096


class AggregationBuilder:
    """Incrementally summarize classified markers before sparse clustering."""

    def __init__(
        self,
        *,
        cluster_window: timedelta,
        schedule_bounds: tuple[int, int] | None = None,
        display_range: tuple[int, int] | None = None,
        outside_limit: int = 50,
        spill_threshold: int = DEFAULT_SPILL_THRESHOLD,
        cluster_materialization_threshold: int = CLUSTER_MATERIALIZATION_THRESHOLD,
    ) -> None:
        self._cluster_window = cluster_window
        self._cluster_window_ns = _cluster_window_ns(cluster_window)
        _validate_schedule_bounds(schedule_bounds)
        _validate_display_range(display_range)
        if outside_limit < 0:
            raise ValueError("outside_limit must not be negative")
        if cluster_materialization_threshold < 0:
            raise ValueError("cluster_materialization_threshold must be non-negative")
        self._schedule_bounds = schedule_bounds
        self._display_range = display_range
        self._outside_limit = outside_limit
        self._cluster_materialization_threshold = cluster_materialization_threshold
        self._marker_store = ChartMarkerStore(spill_threshold=spill_threshold)
        self._finished = False
        self._source_counts: Counter[Source] = Counter()
        self._record_kind_counts: Counter[RecordKind] = Counter()
        self._visual_counts: Counter[tuple[Source, bool]] = Counter()
        self._hidden_before_sources: Counter[Source] = Counter()
        self._hidden_after_sources: Counter[Source] = Counter()
        self._hidden_before_total = 0
        self._hidden_after_total = 0
        self._event_count = 0
        self._within_schedule_count = 0
        self._outside_schedule_count = 0
        self._weekend_count = 0
        self._outside_marker_count = 0
        self._outside_heap: list[tuple[tuple[int, str], int, ClassifiedMarker]] = []
        self._occupied_start_ns: int | None = None
        self._occupied_end_ns: int | None = None

    def add(self, classified: ClassifiedMarker) -> None:
        """Consume one classified marker without retaining its full provenance."""

        if self._finished:
            raise RuntimeError("an aggregation builder cannot be reused after build")
        _validate_classified_marker(classified)
        source, record_kind = marker_dimensions(classified)
        time_of_day_ns = classified.time_of_day_ns

        self._event_count += 1
        self._source_counts[source] += 1
        self._record_kind_counts[record_kind] += 1
        self._occupied_start_ns = (
            time_of_day_ns if self._occupied_start_ns is None else min(self._occupied_start_ns, time_of_day_ns)
        )
        self._occupied_end_ns = (
            time_of_day_ns if self._occupied_end_ns is None else max(self._occupied_end_ns, time_of_day_ns)
        )

        if classified.within_schedule:
            self._within_schedule_count += 1
        else:
            self._outside_schedule_count += 1
            self._outside_marker_count += 1
            _retain_recent(self._outside_heap, classified, self._outside_limit, self._event_count)
        if classified.weekend:
            self._weekend_count += 1

        display_start_ns = self._display_range[0] * NANOSECONDS_PER_MINUTE if self._display_range is not None else None
        display_end_ns = self._display_range[1] * NANOSECONDS_PER_MINUTE if self._display_range is not None else None
        if display_start_ns is None or display_end_ns is None or display_start_ns <= time_of_day_ns < display_end_ns:
            self._visual_counts[(source, classified.within_schedule)] += 1
            self._marker_store.add(
                ChartMarker(
                    marker_id=classified.marker.marker_id,
                    occurred_at_utc_ns=classified.marker.occurred_at_utc_ns,
                    time_of_day_ns=time_of_day_ns,
                    weekday=classified.weekday,
                    source=source,
                    within_schedule=classified.within_schedule,
                )
            )
        elif time_of_day_ns < display_start_ns:
            self._hidden_before_total += 1
            self._hidden_before_sources[source] += 1
        else:
            self._hidden_after_total += 1
            self._hidden_after_sources[source] += 1

    def build(self) -> Aggregation:
        """Finish clustering and return an immutable aggregation snapshot."""

        if self._finished:
            raise RuntimeError("an aggregation builder can only be built once")
        self._finished = True
        display_start, display_end = _resolve_display_range(
            display_range=self._display_range,
            schedule_bounds=self._schedule_bounds,
            occupied_start_ns=self._occupied_start_ns,
            occupied_end_ns=self._occupied_end_ns,
        )
        try:
            layout = cluster_ordered_markers(
                self._marker_store.ordered(),
                self._cluster_window_ns,
                materialization_threshold=self._cluster_materialization_threshold,
            )
        finally:
            self._marker_store.close()
            self._marker_store.clear()
        retained_outside = tuple(item[2] for item in sorted(self._outside_heap, key=lambda item: (item[0], item[1])))
        aggregation = Aggregation(
            cluster_window=self._cluster_window,
            display_start_minute=display_start,
            display_end_minute=display_end,
            display_is_explicit=self._display_range is not None,
            clusters=layout.clusters,
            event_count=self._event_count,
            _displayed_event_count=layout.displayed_event_count,
            within_schedule_count=self._within_schedule_count,
            outside_schedule_count=self._outside_schedule_count,
            weekend_count=self._weekend_count,
            source_counts=freeze_counter(self._source_counts),
            record_kind_counts=freeze_counter(self._record_kind_counts),
            visual_counts=tuple(
                (visual, self._visual_counts[visual]) for visual in VISUAL_ORDER if self._visual_counts[visual]
            ),
            max_cell_event_count=layout.max_cell_event_count,
            has_multi_minute_cluster=layout.has_multi_minute_cluster,
            hidden_before=HiddenMarkers(self._hidden_before_total, freeze_counter(self._hidden_before_sources)),
            hidden_after=HiddenMarkers(self._hidden_after_total, freeze_counter(self._hidden_after_sources)),
            retained_outside_markers=retained_outside,
            outside_marker_count=self._outside_marker_count,
        )
        if aggregation.displayed_event_count + self._hidden_before_total + self._hidden_after_total != self._event_count:
            raise RuntimeError("displayed and hidden marker totals do not reconcile")
        return aggregation

    @property
    def did_spill(self) -> bool:
        """Whether chart sorting crossed the bounded in-memory threshold."""

        return self._marker_store.did_spill


def aggregate_markers(
    markers: Iterable[ClassifiedMarker],
    *,
    cluster_window: timedelta,
    schedule_bounds: tuple[int, int] | None = None,
    display_range: tuple[int, int] | None = None,
    outside_limit: int = 50,
) -> Aggregation:
    """Summarize markers and build globally aligned sparse time clusters.

    Visible events are sorted by exact localized time of day. Each cluster is
    anchored at the earliest unassigned event and contains the half-open range
    ``[anchor, anchor + cluster_window)``. Cropping is applied before
    clustering while summary counts continue to describe the complete input.
    """

    builder = AggregationBuilder(
        cluster_window=cluster_window,
        schedule_bounds=schedule_bounds,
        display_range=display_range,
        outside_limit=outside_limit,
    )
    for classified in markers:
        builder.add(classified)
    return builder.build()


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
