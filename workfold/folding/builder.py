"""Streaming aggregation orchestration."""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Iterable
from datetime import timedelta

from workfold.domain.identity import MarkerIdentity, marker_identity, marker_identity_sort_key
from workfold.domain.observations import ClassifiedMarker, RecordKind, Source, Weekday
from workfold.folding.bands import (
    ClusterAnchor,
    duration_nanoseconds,
    validate_cluster_anchor,
    validate_cluster_window_alignment,
)
from workfold.folding.layout import cluster_ordered_markers
from workfold.folding.markers import VISUAL_ORDER, ChartMarker, marker_dimensions
from workfold.folding.models import (
    MINUTES_PER_DAY,
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_MINUTE,
    Aggregation,
    HiddenMarkers,
    freeze_counter,
)
from workfold.folding.spill import DEFAULT_SPILL_THRESHOLD, ChartMarkerStore

CLUSTER_MATERIALIZATION_THRESHOLD = 4_096


class AggregationBuilder:
    """Incrementally summarize classified markers before sparse clustering."""

    def __init__(
        self,
        *,
        cluster_window: timedelta,
        cluster_anchor: ClusterAnchor = ClusterAnchor.EVENT,
        schedule_bounds: tuple[int, int] | None = None,
        display_range: tuple[int, int] | None = None,
        outside_limit: int = 50,
        retain_git_identities: bool = False,
        hide_days: tuple[Weekday, ...] = (),
        hide_empty_days: tuple[Weekday, ...] = (),
        spill_threshold: int = DEFAULT_SPILL_THRESHOLD,
        cluster_materialization_threshold: int = CLUSTER_MATERIALIZATION_THRESHOLD,
    ) -> None:
        self._cluster_window = cluster_window
        self._cluster_window_ns = _cluster_window_ns(cluster_window)
        self._cluster_anchor = validate_cluster_anchor(cluster_anchor)
        validate_cluster_window_alignment(self._cluster_window, self._cluster_anchor)
        _validate_schedule_bounds(schedule_bounds)
        _validate_display_range(display_range)
        if outside_limit < 0:
            raise ValueError("outside_limit must not be negative")
        if cluster_materialization_threshold < 0:
            raise ValueError("cluster_materialization_threshold must be non-negative")
        self._schedule_bounds = schedule_bounds
        self._display_range = display_range
        self._outside_limit = outside_limit
        self._retain_git_identities = retain_git_identities
        _validate_weekdays(hide_days, "hide_days")
        _validate_weekdays(hide_empty_days, "hide_empty_days")
        self._hide_days = frozenset(hide_days)
        self._hide_empty_days = frozenset(hide_empty_days)
        if self._hide_days == frozenset(Weekday):
            raise ValueError("hide_days must leave at least one weekday column")
        self._cluster_materialization_threshold = cluster_materialization_threshold
        self._marker_store = ChartMarkerStore(spill_threshold=spill_threshold)
        self._finished = False
        self._source_counts: Counter[Source] = Counter()
        self._record_kind_counts: Counter[RecordKind] = Counter()
        self._visual_counts: Counter[tuple[Weekday, Source, bool]] = Counter()
        self._displayed_weekday_counts: Counter[Weekday] = Counter()
        self._identity_ids: dict[MarkerIdentity, int] = {}
        self._identities: list[MarkerIdentity] = []
        self._identity_counts: Counter[tuple[int, Weekday]] = Counter()
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
        self._occupied_start_ns: dict[Weekday, int] = {}
        self._occupied_end_ns: dict[Weekday, int] = {}

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
        weekday = classified.weekday
        self._occupied_start_ns[weekday] = min(self._occupied_start_ns.get(weekday, time_of_day_ns), time_of_day_ns)
        self._occupied_end_ns[weekday] = max(self._occupied_end_ns.get(weekday, time_of_day_ns), time_of_day_ns)

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
            self._displayed_weekday_counts[weekday] += 1
            if weekday not in self._hide_days:
                self._visual_counts[(weekday, source, classified.within_schedule)] += 1
                identity_id = self._register_identity(classified) if self._retain_git_identities else None
                if identity_id is not None:
                    self._identity_counts[(identity_id, weekday)] += 1
                self._marker_store.add(
                    ChartMarker(
                        marker_id=classified.marker.marker_id,
                        occurred_at_utc_ns=classified.marker.occurred_at_utc_ns,
                        time_of_day_ns=time_of_day_ns,
                        weekday=classified.weekday,
                        source=source,
                        within_schedule=classified.within_schedule,
                        identity_id=identity_id,
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
        try:
            return self._build_snapshot()
        finally:
            self._marker_store.close()
            self._marker_store.clear()

    def _build_snapshot(self) -> Aggregation:
        visible_weekdays = self._resolve_visible_weekdays()
        visible_weekday_set = frozenset(visible_weekdays)
        occupied_starts = [self._occupied_start_ns[day] for day in visible_weekdays if day in self._occupied_start_ns]
        occupied_ends = [self._occupied_end_ns[day] for day in visible_weekdays if day in self._occupied_end_ns]
        display_start, display_end = _resolve_display_range(
            display_range=self._display_range,
            schedule_bounds=self._schedule_bounds,
            occupied_start_ns=min(occupied_starts) if occupied_starts else None,
            occupied_end_ns=max(occupied_ends) if occupied_ends else None,
            cluster_window_ns=self._cluster_window_ns,
            cluster_anchor=self._cluster_anchor,
        )
        identities, identity_remap = self._freeze_identities(visible_weekday_set)
        layout = cluster_ordered_markers(
            (
                _remap_marker_identity(marker, identity_remap)
                for marker in self._marker_store.ordered()
                if marker.weekday in visible_weekday_set
            ),
            self._cluster_window_ns,
            self._cluster_anchor,
            materialization_threshold=self._cluster_materialization_threshold,
        )
        retained_outside = tuple(item[2] for item in sorted(self._outside_heap, key=lambda item: (item[0], item[1])))
        visible_visual_counts: Counter[tuple[Source, bool]] = Counter()
        for (weekday, source, within_schedule), count in self._visual_counts.items():
            if weekday in visible_weekday_set:
                visible_visual_counts[(source, within_schedule)] += count
        visible_identity_counts: Counter[int] = Counter()
        for (identity_id, weekday), count in self._identity_counts.items():
            if weekday in visible_weekday_set:
                visible_identity_counts[identity_remap[identity_id]] += count
        hidden_weekday_counts = tuple(
            (weekday, self._displayed_weekday_counts[weekday])
            for weekday in sorted(self._hide_days)
            if self._displayed_weekday_counts[weekday]
        )
        aggregation = Aggregation(
            cluster_window=self._cluster_window,
            cluster_anchor=self._cluster_anchor,
            display_start_minute=display_start,
            display_end_minute=display_end,
            display_is_explicit=self._display_range is not None,
            visible_weekdays=visible_weekdays,
            hidden_weekday_counts=hidden_weekday_counts,
            clusters=layout.clusters,
            event_count=self._event_count,
            _displayed_event_count=layout.displayed_event_count,
            within_schedule_count=self._within_schedule_count,
            outside_schedule_count=self._outside_schedule_count,
            weekend_count=self._weekend_count,
            source_counts=freeze_counter(self._source_counts),
            record_kind_counts=freeze_counter(self._record_kind_counts),
            visual_counts=tuple(
                (visual, visible_visual_counts[visual]) for visual in VISUAL_ORDER if visible_visual_counts[visual]
            ),
            identities=identities,
            identity_counts=tuple(sorted(visible_identity_counts.items())),
            max_cell_event_count=layout.max_cell_event_count,
            has_multi_minute_cluster=layout.has_multi_minute_cluster,
            hidden_before=HiddenMarkers(self._hidden_before_total, freeze_counter(self._hidden_before_sources)),
            hidden_after=HiddenMarkers(self._hidden_after_total, freeze_counter(self._hidden_after_sources)),
            retained_outside_markers=retained_outside,
            outside_marker_count=self._outside_marker_count,
        )
        if (
            aggregation.displayed_event_count
            + aggregation.hidden_weekday_event_count
            + self._hidden_before_total
            + self._hidden_after_total
            != self._event_count
        ):
            raise RuntimeError("displayed and hidden marker totals do not reconcile")
        return aggregation

    def _register_identity(self, classified: ClassifiedMarker) -> int | None:
        identity = marker_identity(classified.marker)
        if identity is None:
            return None
        identity_id = self._identity_ids.get(identity)
        if identity_id is None:
            identity_id = len(self._identities)
            self._identity_ids[identity] = identity_id
            self._identities.append(identity)
        return identity_id

    def _freeze_identities(
        self,
        visible_weekdays: frozenset[Weekday],
    ) -> tuple[tuple[MarkerIdentity, ...], dict[int, int]]:
        visible_ids = {identity_id for identity_id, weekday in self._identity_counts if weekday in visible_weekdays}
        ordered = tuple(
            sorted(
                ((identity_id, self._identities[identity_id]) for identity_id in visible_ids),
                key=lambda item: marker_identity_sort_key(item[1]),
            )
        )
        remap = {old_id: new_id for new_id, (old_id, _identity) in enumerate(ordered)}
        return tuple(identity for _old_id, identity in ordered), remap

    def _resolve_visible_weekdays(self) -> tuple[Weekday, ...]:
        return tuple(
            weekday
            for weekday in Weekday
            if weekday not in self._hide_days
            and not (weekday in self._hide_empty_days and not self._displayed_weekday_counts[weekday])
        )

    def close(self) -> None:
        """Release temporary sorting storage without producing a result."""

        if self._finished:
            return
        self._finished = True
        self._marker_store.close()
        self._marker_store.clear()

    @property
    def did_spill(self) -> bool:
        """Whether chart sorting crossed the bounded in-memory threshold."""

        return self._marker_store.did_spill


def aggregate_markers(
    markers: Iterable[ClassifiedMarker],
    *,
    cluster_window: timedelta,
    cluster_anchor: ClusterAnchor = ClusterAnchor.EVENT,
    schedule_bounds: tuple[int, int] | None = None,
    display_range: tuple[int, int] | None = None,
    outside_limit: int = 50,
    retain_git_identities: bool = False,
    hide_days: tuple[Weekday, ...] = (),
    hide_empty_days: tuple[Weekday, ...] = (),
) -> Aggregation:
    """Summarize markers and build globally aligned sparse time clusters.

    Visible events are sorted by exact localized time of day. Event anchoring
    starts each cluster at the earliest unassigned event; midnight anchoring
    assigns events to fixed clock intervals. Both use half-open bands. Cropping
    and weekday projection are applied before clustering while summary counts
    continue to describe the complete input.
    """

    builder = AggregationBuilder(
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        schedule_bounds=schedule_bounds,
        display_range=display_range,
        outside_limit=outside_limit,
        retain_git_identities=retain_git_identities,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
    )
    try:
        for classified in markers:
            builder.add(classified)
        return builder.build()
    finally:
        builder.close()


def _validate_classified_marker(classified: ClassifiedMarker) -> None:
    if not 0 <= classified.time_of_day_ns < NANOSECONDS_PER_DAY:
        raise ValueError("classified marker local time must fall within one day")


def _remap_marker_identity(marker: ChartMarker, remap: dict[int, int]) -> ChartMarker:
    identity_id = marker.identity_id
    if identity_id is None:
        return marker
    return ChartMarker(
        marker_id=marker.marker_id,
        occurred_at_utc_ns=marker.occurred_at_utc_ns,
        time_of_day_ns=marker.time_of_day_ns,
        weekday=marker.weekday,
        source=marker.source,
        within_schedule=marker.within_schedule,
        identity_id=remap[identity_id],
        count=marker.count,
    )


def _cluster_window_ns(cluster_window: object) -> int:
    if not isinstance(cluster_window, timedelta):
        raise TypeError("cluster_window must be a datetime.timedelta")
    nanoseconds = duration_nanoseconds(cluster_window)
    if not NANOSECONDS_PER_MINUTE <= nanoseconds < NANOSECONDS_PER_DAY:
        raise ValueError("cluster_window must be at least one minute and less than 24 hours")
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
    cluster_window_ns: int,
    cluster_anchor: ClusterAnchor,
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
    if cluster_anchor is ClusterAnchor.MIDNIGHT:
        window_minutes = cluster_window_ns // NANOSECONDS_PER_MINUTE
        start = start // window_minutes * window_minutes
        end = min(MINUTES_PER_DAY, ((end + window_minutes - 1) // window_minutes) * window_minutes)
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


def _validate_weekdays(values: tuple[object, ...], option: str) -> None:
    if any(not isinstance(value, Weekday) for value in values):
        raise TypeError(f"{option} must contain Weekday values")
