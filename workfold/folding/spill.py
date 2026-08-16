"""Bounded-memory sorting for chart markers.

SQLite is used only after the in-memory threshold is crossed. The database
lives in an automatically removed temporary directory and is never Workfold
application state.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import replace
from typing import TypeAlias, cast

from workfold.domain.observations import Source
from workfold.folding.markers import (
    ChartMarker,
    chart_marker_from_row,
    chart_marker_order_key,
    chart_marker_row,
)
from workfold.folding.models import NANOSECONDS_PER_SECOND

# Unique markers are cheaper in a list; repeated visual markers are much
# cheaper in a counted mapping. Sample small windows so the store can choose
# without imposing mapping overhead on ordinary high-cardinality runs.
DEFAULT_SPILL_THRESHOLD = 100_000
DEFAULT_GROUPED_SPILL_THRESHOLD = 32_768
SPILL_INSERT_BATCH = 4_096
GROUPING_SAMPLE_SIZE = 256
GROUPING_SAMPLE_INTERVAL = 8_192
GROUPING_REQUIRED_SAMPLES = 2

MarkerGroupKey: TypeAlias = tuple[int, int, int, int, int, bool, int, str]


class AggregationStorageError(RuntimeError):
    """Temporary aggregation storage could not be created, written, or read."""


class ChartMarkerStore:
    """Sort chart markers in memory, spilling large inputs to temporary SQLite."""

    def __init__(self, *, spill_threshold: int = DEFAULT_SPILL_THRESHOLD) -> None:
        if spill_threshold < 1:
            raise ValueError("spill_threshold must be positive")
        self._spill_threshold = spill_threshold
        self._grouped_spill_threshold = min(spill_threshold, DEFAULT_GROUPED_SPILL_THRESHOLD)
        self._grouping_sample_target = min(GROUPING_SAMPLE_SIZE, spill_threshold + 1)
        self._buffer: list[ChartMarker] | dict[MarkerGroupKey, tuple[ChartMarker, int]] = []
        self._grouped = False
        self._markers_seen = 0
        self._next_sample_at = 0
        self._sample_keys: set[MarkerGroupKey] = set()
        self._sample_count = 0
        self._sample_duplicates = 0
        self._qualifying_samples = 0
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None
        self._did_spill = False

    @property
    def did_spill(self) -> bool:
        return self._did_spill

    def add(self, marker: ChartMarker) -> None:
        if self._grouped:
            self._merge_group(marker)
        else:
            buffer = cast(list[ChartMarker], self._buffer)
            buffer.append(marker)
            if self._connection is None and self._markers_seen >= self._next_sample_at:
                self._sample(marker)

        if self._connection is None:
            rows_seen = self._markers_seen + 1
            if (
                self._grouped
                and len(self._buffer) > self._grouped_spill_threshold
                and not self._grouping_is_worthwhile(rows_seen)
            ):
                self._demote_to_list()
            threshold = self._grouped_spill_threshold if self._grouped else self._spill_threshold
            if len(self._buffer) > threshold:
                # A short custom spill threshold may be reached before a second
                # separated sample. Use the positive evidence already collected
                # before paying the cost of SQLite.
                if not self._grouped and self._qualifying_samples:
                    self._promote_to_grouped()
                    threshold = self._grouped_spill_threshold if self._grouped else self._spill_threshold
                if len(self._buffer) > threshold:
                    self._start_spill()
        elif len(self._buffer) >= SPILL_INSERT_BATCH:
            self._flush()
        self._markers_seen += 1

    def ordered(self) -> Iterable[ChartMarker]:
        connection = self._connection
        if connection is None:
            if self._grouped:
                grouped = cast(dict[MarkerGroupKey, tuple[ChartMarker, int]], self._buffer)
                markers = [replace(marker, count=count) for marker, count in grouped.values()]
                # Retain the compact projections, not both them and the wider
                # grouping index, while sorting and clustering consume them.
                self._buffer = markers
                self._grouped = False
            else:
                markers = cast(list[ChartMarker], self._buffer)
            markers.sort(key=chart_marker_order_key)
            return iter(markers)
        try:
            self._flush()
            connection.commit()
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS chart_marker_grouping ON chart_markers (
                    time_of_day_ns,
                    occurred_at_seconds,
                    occurred_at_remainder_ns,
                    source_rank,
                    weekday,
                    within_schedule,
                    identity_id,
                    group_id,
                    marker_id
                )
                """
            )
            # Aggregate append-only rows at read time. Unlike SQLite UPSERT,
            # these primitives exist in every SQLite supported by Python 3.11.
            cursor = connection.execute(
                """
                SELECT time_of_day_ns, occurred_at_seconds,
                       occurred_at_remainder_ns, source_rank, min(marker_id),
                       weekday, within_schedule, identity_id, sum(event_count)
                  FROM chart_markers
                 GROUP BY time_of_day_ns, occurred_at_seconds,
                          occurred_at_remainder_ns, source_rank, weekday,
                          within_schedule, identity_id, group_id
                 ORDER BY time_of_day_ns, occurred_at_seconds,
                          occurred_at_remainder_ns, source_rank, min(marker_id)
                """
            )
        except (OSError, sqlite3.Error) as error:
            raise _storage_error("prepare temporary aggregation results", error) from error
        return _read_rows(cursor)

    def clear(self) -> None:
        self._buffer.clear()
        self._sample_keys.clear()
        self._sample_count = 0
        self._sample_duplicates = 0
        self._qualifying_samples = 0

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        directory = getattr(self, "_directory", None)
        self._connection = None
        self._directory = None
        failure: BaseException | None = None
        if connection is not None:
            try:
                connection.close()
            except (OSError, sqlite3.Error) as error:
                failure = error
        if directory is not None:
            try:
                directory.cleanup()
            except OSError as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise _storage_error("clean up temporary aggregation storage", failure) from failure

    def _start_spill(self) -> None:
        directory: tempfile.TemporaryDirectory[str] | None = None
        connection: sqlite3.Connection | None = None
        try:
            directory = tempfile.TemporaryDirectory(prefix="workfold-aggregation-")
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
                    within_schedule INTEGER NOT NULL,
                    identity_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    event_count INTEGER NOT NULL CHECK (event_count > 0)
                )
                """
            )
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            if directory is not None:
                with suppress(Exception):
                    directory.cleanup()
            raise _storage_error("initialize temporary aggregation storage", error) from error
        self._directory = directory
        self._connection = connection
        self._did_spill = True
        self._sample_keys.clear()
        self._sample_count = 0
        self._sample_duplicates = 0
        self._qualifying_samples = 0
        self._flush()

    def _flush(self) -> None:
        connection = self._connection
        if connection is None or not self._buffer:
            return
        try:
            connection.executemany(
                "INSERT INTO chart_markers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._buffer_rows(),
            )
        except (OSError, sqlite3.Error) as error:
            raise _storage_error("write temporary aggregation storage", error) from error
        self._buffer.clear()

    def _buffer_rows(self) -> Iterable[tuple[int, int, int, int, str, int, int, int, str, int]]:
        if self._grouped:
            grouped = cast(dict[MarkerGroupKey, tuple[ChartMarker, int]], self._buffer)
            return (
                (*chart_marker_row(marker)[:-1], _marker_group_id(marker), count) for marker, count in grouped.values()
            )
        return (
            (*chart_marker_row(marker)[:-1], _marker_group_id(marker), marker.count)
            for marker in cast(list[ChartMarker], self._buffer)
        )

    def _sample(self, marker: ChartMarker) -> None:
        if self._markers_seen < self._next_sample_at:
            return
        key = _marker_group_key(marker)
        self._sample_count += 1
        if key in self._sample_keys:
            self._sample_duplicates += 1
        else:
            self._sample_keys.add(key)
        if self._sample_count >= self._grouping_sample_target:
            qualifies = self._sample_duplicates * 4 >= self._sample_count
            self._qualifying_samples = self._qualifying_samples + 1 if qualifies else 0
            self._sample_keys.clear()
            self._sample_count = 0
            self._sample_duplicates = 0
            if self._qualifying_samples >= GROUPING_REQUIRED_SAMPLES:
                self._promote_to_grouped()
                return
            self._next_sample_at = self._markers_seen + GROUPING_SAMPLE_INTERVAL

    def _promote_to_grouped(self) -> None:
        markers = cast(list[ChartMarker], self._buffer)
        self._buffer = {}
        self._grouped = True
        self._sample_keys.clear()
        self._sample_count = 0
        self._sample_duplicates = 0
        self._qualifying_samples = 0
        for marker in markers:
            self._merge_group(marker)
        if not self._grouping_is_worthwhile(self._markers_seen + 1):
            self._demote_to_list()

    def _grouping_is_worthwhile(self, rows_seen: int) -> bool:
        grouped = cast(dict[MarkerGroupKey, tuple[ChartMarker, int]], self._buffer)
        return len(grouped) * 4 <= rows_seen * 3

    def _demote_to_list(self) -> None:
        grouped = cast(dict[MarkerGroupKey, tuple[ChartMarker, int]], self._buffer)
        self._buffer = [replace(marker, count=count) for marker, count in grouped.values()]
        self._grouped = False
        self._sample_keys.clear()
        self._sample_count = 0
        self._sample_duplicates = 0
        self._qualifying_samples = 0
        self._next_sample_at = self._markers_seen + GROUPING_SAMPLE_INTERVAL

    def _merge_group(self, marker: ChartMarker) -> None:
        grouped = cast(dict[MarkerGroupKey, tuple[ChartMarker, int]], self._buffer)
        key = _marker_group_key(marker)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = (marker, marker.count)
            return
        representative, count = existing
        if marker.marker_id < representative.marker_id:
            representative = marker
        grouped[key] = (representative, count + marker.count)

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()


def _marker_group_id(marker: ChartMarker) -> str:
    """Keep identity markers ordered individually; compact equivalent visuals."""

    return marker.marker_id if marker.identity_id is not None else ""


def _marker_group_key(marker: ChartMarker) -> MarkerGroupKey:
    seconds, remainder_ns = divmod(marker.occurred_at_utc_ns, NANOSECONDS_PER_SECOND)
    return (
        marker.time_of_day_ns,
        seconds,
        remainder_ns,
        0 if marker.source is Source.GIT else 1,
        int(marker.weekday),
        marker.within_schedule,
        -1 if marker.identity_id is None else marker.identity_id,
        _marker_group_id(marker),
    )


def _read_rows(cursor: sqlite3.Cursor) -> Iterable[ChartMarker]:
    try:
        for row in cursor:
            yield chart_marker_from_row(row)
    except (OSError, sqlite3.Error) as error:
        raise _storage_error("read temporary aggregation storage", error) from error


def _storage_error(action: str, error: BaseException) -> AggregationStorageError:
    return AggregationStorageError(f"could not {action}: {error}")
