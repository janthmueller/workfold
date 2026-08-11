"""Anchored clustering and compact immutable chart storage."""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast, overload

from workfold.aggregation.markers import VISUAL_ORDER, CellRunBuilder, ChartMarker
from workfold.aggregation.models import NANOSECONDS_PER_MINUTE, ClusterCell, MarkerRun, TimeCluster
from workfold.models import Weekday


class CompactClusterSequence(Sequence[TimeCluster]):
    """Primitive-array storage with lazy immutable cluster decoding."""

    __slots__ = (
        "_cell_compacted",
        "_cell_run_offsets",
        "_cell_weekdays",
        "_cluster_cell_offsets",
        "_ends",
        "_run_codes",
        "_run_counts",
        "_run_identity_ids",
        "_starts",
        "_frozen",
    )

    def __init__(self) -> None:
        self._starts = array("Q")
        self._ends = array("Q")
        self._cluster_cell_offsets = array("Q", (0,))
        self._cell_weekdays = array("B")
        self._cell_compacted = bytearray()
        self._cell_run_offsets = array("Q", (0,))
        self._run_codes = array("B")
        self._run_counts = array("Q")
        self._run_identity_ids = array("I")
        self._frozen = False

    def append_cluster(self, start_time_ns: int, end_time_ns: int, cells: Iterable[ClusterCell]) -> int:
        """Append one cluster and return its largest cell event count."""

        if self._frozen:
            raise RuntimeError("compact cluster storage is immutable after finalization")
        self._starts.append(start_time_ns)
        self._ends.append(end_time_ns)
        largest_cell = 0
        for cell in cells:
            self._cell_weekdays.append(int(cell.weekday))
            self._cell_compacted.append(int(cell.compacted))
            largest_cell = max(largest_cell, cell.event_count)
            for run in cell.runs:
                self._run_codes.append(VISUAL_ORDER.index((run.source, run.within_schedule)))
                self._run_counts.append(run.count)
                self._run_identity_ids.append(0 if run.identity_id is None else run.identity_id + 1)
            self._cell_run_offsets.append(len(self._run_codes))
        self._cluster_cell_offsets.append(len(self._cell_weekdays))
        return largest_cell

    def freeze(self) -> None:
        self._frozen = True

    def __len__(self) -> int:
        return len(self._starts)

    @overload
    def __getitem__(self, index: int) -> TimeCluster: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[TimeCluster, ...]: ...

    def __getitem__(self, index: int | slice) -> TimeCluster | tuple[TimeCluster, ...]:
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("cluster index out of range")

        cells: list[ClusterCell] = []
        cell_start = self._cluster_cell_offsets[index]
        cell_end = self._cluster_cell_offsets[index + 1]
        for cell_index in range(cell_start, cell_end):
            runs = tuple(
                MarkerRun(
                    *VISUAL_ORDER[self._run_codes[run_index]],
                    self._run_counts[run_index],
                    None if self._run_identity_ids[run_index] == 0 else self._run_identity_ids[run_index] - 1,
                )
                for run_index in range(
                    self._cell_run_offsets[cell_index],
                    self._cell_run_offsets[cell_index + 1],
                )
            )
            cells.append(
                ClusterCell(
                    Weekday(self._cell_weekdays[cell_index]),
                    runs,
                    bool(self._cell_compacted[cell_index]),
                )
            )
        return TimeCluster(self._starts[index], self._ends[index], tuple(cells))

    def __iter__(self) -> Iterator[TimeCluster]:
        return (self[index] for index in range(len(self)))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CompactClusterSequence):
            return all(
                first == second
                for first, second in (
                    (self._starts, other._starts),
                    (self._ends, other._ends),
                    (self._cluster_cell_offsets, other._cluster_cell_offsets),
                    (self._cell_weekdays, other._cell_weekdays),
                    (self._cell_compacted, other._cell_compacted),
                    (self._cell_run_offsets, other._cell_run_offsets),
                    (self._run_codes, other._run_codes),
                    (self._run_counts, other._run_counts),
                    (self._run_identity_ids, other._run_identity_ids),
                )
            )
        if isinstance(other, Sequence):
            candidates = cast(Sequence[object], other)
            return len(self) == len(candidates) and all(
                cluster == candidate for cluster, candidate in zip(self, candidates, strict=True)
            )
        return NotImplemented


@dataclass(frozen=True, slots=True)
class ClusteredLayout:
    clusters: Sequence[TimeCluster]
    displayed_event_count: int
    max_cell_event_count: int
    has_multi_minute_cluster: bool


def cluster_ordered_markers(
    markers: Iterable[ChartMarker],
    window_ns: int,
    *,
    materialization_threshold: int,
) -> ClusteredLayout:
    """Cluster a sorted stream into compact storage and small tuple snapshots."""

    compact = CompactClusterSequence()
    anchor: int | None = None
    end_time = 0
    by_weekday: dict[Weekday, CellRunBuilder] = {}
    displayed_event_count = 0
    max_cell_event_count = 0
    has_multi_minute_cluster = False

    def finish_cluster() -> None:
        nonlocal displayed_event_count, has_multi_minute_cluster, max_cell_event_count
        if anchor is None:
            return
        cells = tuple(by_weekday[weekday].build(weekday) for weekday in sorted(by_weekday))
        displayed_event_count += sum(cell.event_count for cell in cells)
        max_cell_event_count = max(
            max_cell_event_count,
            compact.append_cluster(start_time_ns=anchor, end_time_ns=end_time, cells=cells),
        )
        has_multi_minute_cluster |= anchor // NANOSECONDS_PER_MINUTE != end_time // NANOSECONDS_PER_MINUTE

    for marker in markers:
        if anchor is None or marker.time_of_day_ns >= anchor + window_ns:
            finish_cluster()
            anchor = marker.time_of_day_ns
            by_weekday = {}
        end_time = marker.time_of_day_ns
        by_weekday.setdefault(marker.weekday, CellRunBuilder()).add(marker)
    finish_cluster()
    compact.freeze()
    clusters: Sequence[TimeCluster] = tuple(compact) if len(compact) <= materialization_threshold else compact
    return ClusteredLayout(
        clusters=clusters,
        displayed_event_count=displayed_event_count,
        max_cell_event_count=max_cell_event_count,
        has_multi_minute_cluster=has_multi_minute_cluster,
    )
