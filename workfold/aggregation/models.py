"""Public immutable models produced by activity aggregation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from workfold.identities import MarkerIdentity
from workfold.models import ClassifiedMarker, RecordKind, Source, Weekday

MINUTES_PER_DAY = 24 * 60
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND
NANOSECONDS_PER_DAY = 24 * 60 * NANOSECONDS_PER_MINUTE

_CountKey = TypeVar("_CountKey", Source, RecordKind)


@dataclass(frozen=True, slots=True)
class MarkerRun:
    """One ordered run of visually equivalent events."""

    source: Source
    within_schedule: bool
    count: int
    identity_id: int | None = None

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("a marker run must contain at least one event")
        if self.identity_id is not None and self.identity_id < 0:
            raise ValueError("a marker identity ID must not be negative")
        if self.identity_id is not None and self.source is not Source.GIT:
            raise ValueError("only Git marker runs may carry an identity ID")


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
            first.source is second.source
            and first.within_schedule == second.within_schedule
            and first.identity_id == second.identity_id
            for first, second in zip(self.runs, self.runs[1:])
        ):
            raise ValueError("adjacent equivalent marker runs must be coalesced")

    @property
    def event_count(self) -> int:
        """Return the number of individually renderable events in this cell."""

        return sum(run.count for run in self.runs)


@dataclass(frozen=True, slots=True)
class TimeCluster:
    """One globally aligned, anchored occupied wall-clock row.

    ``start_time_ns`` and ``end_time_ns`` are the first and last observed
    local times, not an estimate of activity duration. The clustering window
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
    """Projected sparse rows plus exact full-scope summary state."""

    cluster_window: timedelta
    display_start_minute: int
    display_end_minute: int
    display_is_explicit: bool
    visible_weekdays: tuple[Weekday, ...]
    hidden_weekday_counts: tuple[tuple[Weekday, int], ...]
    clusters: Sequence[TimeCluster]
    event_count: int
    _displayed_event_count: int
    within_schedule_count: int
    outside_schedule_count: int
    weekend_count: int
    source_counts: tuple[tuple[Source, int], ...]
    record_kind_counts: tuple[tuple[RecordKind, int], ...]
    visual_counts: tuple[tuple[tuple[Source, bool], int], ...]
    identities: tuple[MarkerIdentity, ...]
    identity_counts: tuple[tuple[int, int], ...]
    max_cell_event_count: int
    has_multi_minute_cluster: bool
    hidden_before: HiddenMarkers
    hidden_after: HiddenMarkers
    retained_outside_markers: tuple[ClassifiedMarker, ...]
    outside_marker_count: int

    def __post_init__(self) -> None:
        if self.visible_weekdays != tuple(sorted(set(self.visible_weekdays))):
            raise ValueError("visible weekdays must be unique and ordered")
        if any(count < 1 for _weekday, count in self.hidden_weekday_counts):
            raise ValueError("hidden weekday counts must be positive")
        hidden_weekdays = tuple(weekday for weekday, _count in self.hidden_weekday_counts)
        if hidden_weekdays != tuple(sorted(set(hidden_weekdays))):
            raise ValueError("hidden weekday counts must be unique and ordered")
        if set(self.visible_weekdays) & set(hidden_weekdays):
            raise ValueError("visible and explicitly hidden occupied weekdays cannot overlap")
        if sum(count for _visual, count in self.visual_counts) != self._displayed_event_count:
            raise ValueError("visual counts must reconcile with displayed markers")
        if (
            self._displayed_event_count
            + self.hidden_weekday_event_count
            + self.hidden_before.total
            + self.hidden_after.total
            != self.event_count
        ):
            raise ValueError("displayed and hidden markers must reconcile with the full event count")
        if any(count < 1 for _identity_id, count in self.identity_counts):
            raise ValueError("identity counts must be positive")
        identity_ids = tuple(identity_id for identity_id, _count in self.identity_counts)
        if identity_ids != tuple(range(len(self.identities))):
            raise ValueError("identity counts must cover the ordered identity registry exactly")
        if self.identities and sum(count for _identity_id, count in self.identity_counts) != sum(
            count for (source, _within_schedule), count in self.visual_counts if source is Source.GIT
        ):
            raise ValueError("identity counts must reconcile with displayed Git markers")

    @property
    def outside_omitted_count(self) -> int:
        """Return outside markers not retained because of the list limit."""

        return self.outside_marker_count - len(self.retained_outside_markers)

    @property
    def displayed_event_count(self) -> int:
        """Return the count represented by the sparse chart rows."""

        return self._displayed_event_count

    @property
    def hidden_weekday_event_count(self) -> int:
        """Return events omitted from the matrix by explicit weekday hiding."""

        return sum(count for _weekday, count in self.hidden_weekday_counts)

    def count_for_source(self, source: Source) -> int:
        """Return the total marker count for *source*."""

        return _lookup_count(self.source_counts, source)

    def count_for_record_kind(self, record_kind: RecordKind) -> int:
        """Return the total marker count for *record_kind*."""

        return _lookup_count(self.record_kind_counts, record_kind)

    def count_for_visual(self, source: Source, within_schedule: bool) -> int:
        """Return the number of markers sharing one rendered visual role."""

        return next(
            (
                count
                for (candidate_source, candidate_schedule), count in self.visual_counts
                if candidate_source is source and candidate_schedule == within_schedule
            ),
            0,
        )

    def count_for_identity(self, identity_id: int) -> int:
        """Return the displayed marker count for one compact identity ID."""

        return next((count for candidate, count in self.identity_counts if candidate == identity_id), 0)


def freeze_counter(counter: dict[_CountKey, int]) -> tuple[tuple[_CountKey, int], ...]:
    """Freeze a source or record-kind counter in deterministic enum order."""

    return tuple(sorted(((key, count) for key, count in counter.items() if count), key=lambda item: item[0].value))


def _lookup_count(pairs: tuple[tuple[_CountKey, int], ...], key: _CountKey) -> int:
    return next((count for candidate, count in pairs if candidate is key), 0)
