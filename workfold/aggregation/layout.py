"""Anchored clustering and compact immutable chart storage."""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast, overload

from workfold.aggregation.markers import VISUAL_ORDER, CellRunBuilder, ChartMarker
from workfold.aggregation.models import (
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_MINUTE,
    ClusterCell,
    MarkerRun,
    TimeCluster,
)
from workfold.models import Weekday
from workfold.time_bands import ClusterAnchor


class CompactClusterSequence(Sequence[TimeCluster]):
    """Primitive-array storage with lazy immutable cluster decoding."""

    __slots__ = (
        "_cell_compacted",
        "_cell_run_offsets",
        "_cell_weekdays",
        "_cluster_cell_offsets",
        "_band_ends",
        "_band_starts",
        "_observed_ends",
        "_observed_starts",
        "_run_codes",
        "_run_counts",
        "_run_identity_ids",
        "_frozen",
    )

    def __init__(self) -> None:
        self._band_starts = array("Q")
        self._band_ends = array("Q")
        self._observed_starts = array("Q")
        self._observed_ends = array("Q")
        self._cluster_cell_offsets = array("Q", (0,))
        self._cell_weekdays = array("B")
        self._cell_compacted = bytearray()
        self._cell_run_offsets = array("Q", (0,))
        self._run_codes = array("B")
        self._run_counts = array("Q")
        self._run_identity_ids = array("I")
        self._frozen = False

    def append_cluster(
        self,
        band_start_time_ns: int,
        band_end_time_ns: int,
        observed_start_time_ns: int,
        observed_end_time_ns: int,
        cells: Iterable[ClusterCell],
    ) -> int:
        """Append one cluster and return its largest cell event count."""

        if self._frozen:
            raise RuntimeError("compact cluster storage is immutable after finalization")
        self._band_starts.append(band_start_time_ns)
        self._band_ends.append(band_end_time_ns)
        self._observed_starts.append(observed_start_time_ns)
        self._observed_ends.append(observed_end_time_ns)
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
        return len(self._band_starts)

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
        return TimeCluster(
            start_time_ns=self._observed_starts[index],
            end_time_ns=self._observed_ends[index],
            cells=tuple(cells),
            band_start_time_ns=self._band_starts[index],
            band_end_time_ns=self._band_ends[index],
        )

    def __iter__(self) -> Iterator[TimeCluster]:
        return (self[index] for index in range(len(self)))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CompactClusterSequence):
            return all(
                first == second
                for first, second in (
                    (self._band_starts, other._band_starts),
                    (self._band_ends, other._band_ends),
                    (self._observed_starts, other._observed_starts),
                    (self._observed_ends, other._observed_ends),
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
    anchor_mode: ClusterAnchor,
    *,
    materialization_threshold: int,
) -> ClusteredLayout:
    """Cluster a sorted stream into compact storage and small tuple snapshots."""

    compact = CompactClusterSequence()
    band_start: int | None = None
    band_end = 0
    observed_start = 0
    observed_end = 0
    by_weekday: dict[Weekday, CellRunBuilder] = {}
    displayed_event_count = 0
    max_cell_event_count = 0
    has_multi_minute_cluster = False

    def finish_cluster() -> None:
        nonlocal displayed_event_count, has_multi_minute_cluster, max_cell_event_count
        if band_start is None:
            return
        cells = tuple(by_weekday[weekday].build(weekday) for weekday in sorted(by_weekday))
        displayed_event_count += sum(cell.event_count for cell in cells)
        max_cell_event_count = max(
            max_cell_event_count,
            compact.append_cluster(
                band_start_time_ns=band_start,
                band_end_time_ns=band_end,
                observed_start_time_ns=observed_start,
                observed_end_time_ns=observed_end,
                cells=cells,
            ),
        )
        has_multi_minute_cluster |= observed_start // NANOSECONDS_PER_MINUTE != observed_end // NANOSECONDS_PER_MINUTE

    for marker in markers:
        marker_band_start = (
            marker.time_of_day_ns
            if anchor_mode is ClusterAnchor.EVENT
            else marker.time_of_day_ns // window_ns * window_ns
        )
        starts_new_band = band_start is None or (
            marker.time_of_day_ns >= band_end if anchor_mode is ClusterAnchor.EVENT else marker_band_start != band_start
        )
        if starts_new_band:
            finish_cluster()
            band_start = marker_band_start
            band_end = min(band_start + window_ns, NANOSECONDS_PER_DAY)
            observed_start = marker.time_of_day_ns
            by_weekday = {}
        observed_end = marker.time_of_day_ns
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
