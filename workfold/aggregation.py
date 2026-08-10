"""Renderer-neutral sparse weekly layout and activity summaries."""

from __future__ import annotations

import heapq
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TypeVar

from workfold.models import ClassifiedMarker, RecordKind, Source, Weekday

MINUTES_PER_DAY = 24 * 60
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND
NANOSECONDS_PER_DAY = 24 * 60 * NANOSECONDS_PER_MINUTE
_CountKey = TypeVar("_CountKey", Source, RecordKind)
_DEFAULT_SPILL_THRESHOLD = 100_000
_SPILL_INSERT_BATCH = 4_096
_RUN_COMPACTION_THRESHOLD = 256


@dataclass(frozen=True, slots=True)
class MarkerRun:
    """One ordered run of visually equivalent events.

    A chart needs source, schedule classification, and exact multiplicity, but
    it does not need every event's complete provenance graph. Consecutive
    equivalent markers are therefore retained as one run without changing
    their visual order or event count.
    """

    source: Source
    within_schedule: bool
    count: int

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("a marker run must contain at least one event")


@dataclass(frozen=True, slots=True)
class ClusterCell:
    """The exact visual event sequence for one weekday in a time cluster."""

    weekday: Weekday
    runs: tuple[MarkerRun, ...]
    compacted: bool = False

    def __post_init__(self) -> None:
        if not self.runs:
            raise ValueError("a cluster cell must contain at least one marker")
        if any(
            first.source is second.source and first.within_schedule == second.within_schedule
            for first, second in zip(self.runs, self.runs[1:])
        ):
            raise ValueError("adjacent equivalent marker runs must be coalesced")

    @property
    def event_count(self) -> int:
        """Return the number of individually renderable events in this cell."""

        return sum(run.count for run in self.runs)


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


@dataclass(frozen=True, slots=True)
class _ChartMarker:
    """Minimal renderer-neutral marker retained for sorting and clustering."""

    marker_id: str
    occurred_at_utc_ns: int
    time_of_day_ns: int
    weekday: Weekday
    source: Source
    within_schedule: bool


@dataclass(slots=True)
class _CellRunBuilder:
    """Retain short visual sequences and compact pathologically busy cells."""

    runs: list[MarkerRun] = field(default_factory=lambda: [])
    counts: Counter[tuple[Source, bool]] | None = None

    def add(self, marker: _ChartMarker) -> None:
        if self.counts is not None:
            self.counts[(marker.source, marker.within_schedule)] += 1
            return
        if self.runs and _same_visual(self.runs[-1], marker):
            previous = self.runs[-1]
            self.runs[-1] = MarkerRun(previous.source, previous.within_schedule, previous.count + 1)
        else:
            self.runs.append(MarkerRun(marker.source, marker.within_schedule, 1))
        if len(self.runs) > _RUN_COMPACTION_THRESHOLD:
            self.counts = Counter()
            for run in self.runs:
                self.counts[(run.source, run.within_schedule)] += run.count
            self.runs.clear()

    def build(self, weekday: Weekday) -> ClusterCell:
        if self.counts is None:
            return ClusterCell(weekday, tuple(self.runs))
        runs = tuple(
            MarkerRun(source, within_schedule, self.counts[(source, within_schedule)])
            for source, within_schedule in _VISUAL_ORDER
            if self.counts[(source, within_schedule)]
        )
        return ClusterCell(weekday, runs, compacted=True)


_VISUAL_ORDER = (
    (Source.GIT, True),
    (Source.FILESYSTEM, True),
    (Source.GIT, False),
    (Source.FILESYSTEM, False),
)


class AggregationBuilder:
    """Incrementally summarize classified markers before sparse clustering.

    Full provenance is retained only for the bounded outside-event list. The
    chart path projects every other event to a compact marker immediately and
    later coalesces adjacent visual equivalents into :class:`MarkerRun` values.
    """

    def __init__(
        self,
        *,
        cluster_window: timedelta,
        schedule_bounds: tuple[int, int] | None = None,
        display_range: tuple[int, int] | None = None,
        outside_limit: int = 50,
        spill_threshold: int = _DEFAULT_SPILL_THRESHOLD,
    ) -> None:
        self._cluster_window = cluster_window
        self._cluster_window_ns = _cluster_window_ns(cluster_window)
        _validate_schedule_bounds(schedule_bounds)
        _validate_display_range(display_range)
        if outside_limit < 0:
            raise ValueError("outside_limit must not be negative")
        if spill_threshold < 1:
            raise ValueError("spill_threshold must be positive")
        self._schedule_bounds = schedule_bounds
        self._display_range = display_range
        self._outside_limit = outside_limit
        self._spill_threshold = spill_threshold
        self._visible_markers: list[_ChartMarker] = []
        self._spill_directory: tempfile.TemporaryDirectory[str] | None = None
        self._spill_connection: sqlite3.Connection | None = None
        self._did_spill = False
        self._finished = False
        self._source_counts: Counter[Source] = Counter()
        self._record_kind_counts: Counter[RecordKind] = Counter()
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
        source, record_kind = _marker_dimensions(classified)
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
            self._add_visible_marker(
                _ChartMarker(
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
            clusters = _cluster_ordered_markers(self._ordered_visible_markers(), self._cluster_window_ns)
        finally:
            self._cleanup_spill()
        retained_outside = tuple(item[2] for item in sorted(self._outside_heap, key=lambda item: (item[0], item[1])))
        aggregation = Aggregation(
            cluster_window=self._cluster_window,
            display_start_minute=display_start,
            display_end_minute=display_end,
            display_is_explicit=self._display_range is not None,
            clusters=clusters,
            event_count=self._event_count,
            within_schedule_count=self._within_schedule_count,
            outside_schedule_count=self._outside_schedule_count,
            weekend_count=self._weekend_count,
            source_counts=_freeze_counter(self._source_counts),
            record_kind_counts=_freeze_counter(self._record_kind_counts),
            hidden_before=HiddenMarkers(
                self._hidden_before_total,
                _freeze_counter(self._hidden_before_sources),
            ),
            hidden_after=HiddenMarkers(
                self._hidden_after_total,
                _freeze_counter(self._hidden_after_sources),
            ),
            retained_outside_markers=retained_outside,
            outside_marker_count=self._outside_marker_count,
        )
        if (
            aggregation.displayed_event_count + self._hidden_before_total + self._hidden_after_total
            != self._event_count
        ):
            raise RuntimeError("displayed and hidden marker totals do not reconcile")
        return aggregation

    @property
    def did_spill(self) -> bool:
        """Whether chart sorting crossed the bounded in-memory threshold."""

        return self._did_spill

    def _add_visible_marker(self, marker: _ChartMarker) -> None:
        self._visible_markers.append(marker)
        if self._spill_connection is None and len(self._visible_markers) > self._spill_threshold:
            self._start_spill()
        elif self._spill_connection is not None and len(self._visible_markers) >= _SPILL_INSERT_BATCH:
            self._flush_spill_buffer()

    def _start_spill(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="workfold-aggregation-")
        try:
            connection = sqlite3.connect(f"{directory.name}/markers.sqlite3")
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-8192")
            connection.execute(
                """
                CREATE TABLE chart_markers (
                    time_of_day_ns INTEGER NOT NULL,
                    occurred_at_seconds INTEGER NOT NULL,
                    occurred_at_remainder_ns INTEGER NOT NULL,
                    source_rank INTEGER NOT NULL,
                    marker_id TEXT NOT NULL,
                    weekday INTEGER NOT NULL,
                    within_schedule INTEGER NOT NULL
                )
                """
            )
        except Exception:
            directory.cleanup()
            raise
        self._spill_directory = directory
        self._spill_connection = connection
        self._did_spill = True
        self._flush_spill_buffer()

    def _flush_spill_buffer(self) -> None:
        connection = self._spill_connection
        if connection is None or not self._visible_markers:
            return
        connection.executemany(
            "INSERT INTO chart_markers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_chart_marker_row(marker) for marker in self._visible_markers),
        )
        self._visible_markers.clear()

    def _ordered_visible_markers(self) -> Iterable[_ChartMarker]:
        connection = self._spill_connection
        if connection is None:
            return iter(sorted(self._visible_markers, key=_chart_marker_order_key))
        self._flush_spill_buffer()
        connection.commit()
        connection.execute(
            """
            CREATE INDEX chart_marker_order ON chart_markers (
                time_of_day_ns,
                occurred_at_seconds,
                occurred_at_remainder_ns,
                source_rank,
                marker_id
            )
            """
        )
        cursor = connection.execute(
            """
            SELECT time_of_day_ns, occurred_at_seconds,
                   occurred_at_remainder_ns, source_rank, marker_id,
                   weekday, within_schedule
              FROM chart_markers
             ORDER BY time_of_day_ns, occurred_at_seconds,
                      occurred_at_remainder_ns, source_rank, marker_id
            """
        )
        return (_chart_marker_from_row(row) for row in cursor)

    def _cleanup_spill(self) -> None:
        connection = getattr(self, "_spill_connection", None)
        if connection is not None:
            connection.close()
            self._spill_connection = None
        directory = getattr(self, "_spill_directory", None)
        if directory is not None:
            directory.cleanup()
            self._spill_directory = None

    def __del__(self) -> None:
        self._cleanup_spill()


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

    builder = AggregationBuilder(
        cluster_window=cluster_window,
        schedule_bounds=schedule_bounds,
        display_range=display_range,
        outside_limit=outside_limit,
    )
    for classified in markers:
        builder.add(classified)
    return builder.build()


def _cluster_ordered_markers(markers: Iterable[_ChartMarker], window_ns: int) -> tuple[TimeCluster, ...]:
    """Cluster a pre-sorted stream while retaining only compact cell runs."""

    clusters: list[TimeCluster] = []
    anchor: int | None = None
    end_time = 0
    by_weekday: dict[Weekday, _CellRunBuilder] = {}

    def finish_cluster() -> None:
        if anchor is None:
            return
        cells = tuple(by_weekday[weekday].build(weekday) for weekday in sorted(by_weekday))
        clusters.append(
            TimeCluster(
                start_time_ns=anchor,
                end_time_ns=end_time,
                cells=cells,
            )
        )

    for marker in markers:
        if anchor is None or marker.time_of_day_ns >= anchor + window_ns:
            finish_cluster()
            anchor = marker.time_of_day_ns
            by_weekday = {}
        end_time = marker.time_of_day_ns
        by_weekday.setdefault(marker.weekday, _CellRunBuilder()).add(marker)
    finish_cluster()
    return tuple(clusters)


def _same_visual(run: MarkerRun, marker: _ChartMarker) -> bool:
    return run.source is marker.source and run.within_schedule == marker.within_schedule


def _chart_marker_row(marker: _ChartMarker) -> tuple[int, int, int, int, str, int, int]:
    seconds, remainder_ns = divmod(marker.occurred_at_utc_ns, NANOSECONDS_PER_SECOND)
    return (
        marker.time_of_day_ns,
        seconds,
        remainder_ns,
        0 if marker.source is Source.GIT else 1,
        marker.marker_id,
        int(marker.weekday),
        int(marker.within_schedule),
    )


def _chart_marker_from_row(row: tuple[int, int, int, int, str, int, int]) -> _ChartMarker:
    time_of_day_ns, seconds, remainder_ns, source_rank, marker_id, weekday, within_schedule = row
    return _ChartMarker(
        marker_id=marker_id,
        occurred_at_utc_ns=seconds * NANOSECONDS_PER_SECOND + remainder_ns,
        time_of_day_ns=time_of_day_ns,
        weekday=Weekday(weekday),
        source=Source.GIT if source_rank == 0 else Source.FILESYSTEM,
        within_schedule=bool(within_schedule),
    )


def _chart_marker_order_key(marker: _ChartMarker) -> tuple[int, int, int, str]:
    return (
        marker.time_of_day_ns,
        marker.occurred_at_utc_ns,
        0 if marker.source is Source.GIT else 1,
        marker.marker_id,
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
    "AggregationBuilder",
    "ClusterCell",
    "HiddenMarkers",
    "MarkerRun",
    "TimeCluster",
    "aggregate_markers",
]
