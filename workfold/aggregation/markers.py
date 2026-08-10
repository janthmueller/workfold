"""Compact marker projections used only while building a chart."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from workfold.aggregation.models import NANOSECONDS_PER_SECOND, ClusterCell, MarkerRun
from workfold.models import ClassifiedMarker, RecordKind, Source, Weekday

RUN_COMPACTION_THRESHOLD = 256
VISUAL_ORDER = (
    (Source.GIT, True),
    (Source.FILESYSTEM, True),
    (Source.GIT, False),
    (Source.FILESYSTEM, False),
)


@dataclass(frozen=True, slots=True)
class ChartMarker:
    """Minimal renderer-neutral marker retained for sorting and clustering."""

    marker_id: str
    occurred_at_utc_ns: int
    time_of_day_ns: int
    weekday: Weekday
    source: Source
    within_schedule: bool


@dataclass(slots=True)
class CellRunBuilder:
    """Retain short visual sequences and compact pathologically busy cells."""

    runs: list[MarkerRun] = field(default_factory=lambda: [])
    counts: Counter[tuple[Source, bool]] | None = None

    def add(self, marker: ChartMarker) -> None:
        if self.counts is not None:
            self.counts[(marker.source, marker.within_schedule)] += 1
            return
        if self.runs and same_visual(self.runs[-1], marker):
            previous = self.runs[-1]
            self.runs[-1] = MarkerRun(previous.source, previous.within_schedule, previous.count + 1)
        else:
            self.runs.append(MarkerRun(marker.source, marker.within_schedule, 1))
        if len(self.runs) > RUN_COMPACTION_THRESHOLD:
            self.counts = Counter()
            for run in self.runs:
                self.counts[(run.source, run.within_schedule)] += run.count
            self.runs.clear()

    def build(self, weekday: Weekday) -> ClusterCell:
        if self.counts is None:
            return ClusterCell(weekday, tuple(self.runs))
        runs = tuple(
            MarkerRun(source, within_schedule, self.counts[(source, within_schedule)])
            for source, within_schedule in VISUAL_ORDER
            if self.counts[(source, within_schedule)]
        )
        return ClusterCell(weekday, runs, compacted=True)


def same_visual(run: MarkerRun, marker: ChartMarker) -> bool:
    return run.source is marker.source and run.within_schedule == marker.within_schedule


def chart_marker_row(marker: ChartMarker) -> tuple[int, int, int, int, str, int, int]:
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


def chart_marker_from_row(row: tuple[int, int, int, int, str, int, int]) -> ChartMarker:
    time_of_day_ns, seconds, remainder_ns, source_rank, marker_id, weekday, within_schedule = row
    return ChartMarker(
        marker_id=marker_id,
        occurred_at_utc_ns=seconds * NANOSECONDS_PER_SECOND + remainder_ns,
        time_of_day_ns=time_of_day_ns,
        weekday=Weekday(weekday),
        source=Source.GIT if source_rank == 0 else Source.FILESYSTEM,
        within_schedule=bool(within_schedule),
    )


def chart_marker_order_key(marker: ChartMarker) -> tuple[int, int, int, str]:
    return (
        marker.time_of_day_ns,
        marker.occurred_at_utc_ns,
        0 if marker.source is Source.GIT else 1,
        marker.marker_id,
    )


def marker_dimensions(classified: ClassifiedMarker) -> tuple[Source, RecordKind]:
    observations = classified.marker.observations
    if not observations:
        raise ValueError("an activity marker must contain at least one observation")
    source = observations[0].origin.source
    record_kind = observations[0].origin.record_kind
    for observation in observations[1:]:
        if observation.origin.source is not source or observation.origin.record_kind is not record_kind:
            raise ValueError("coalesced marker observations must share source and record kind")
    return source, record_kind
