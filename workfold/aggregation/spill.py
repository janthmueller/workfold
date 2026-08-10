"""Bounded-memory sorting for chart markers.

SQLite is used only after the in-memory threshold is crossed. The database
lives in an automatically removed temporary directory and is never Workfold
application state.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable

from workfold.aggregation.markers import (
    ChartMarker,
    chart_marker_from_row,
    chart_marker_order_key,
    chart_marker_row,
)

DEFAULT_SPILL_THRESHOLD = 100_000
SPILL_INSERT_BATCH = 4_096


class ChartMarkerStore:
    """Sort chart markers in memory, spilling large inputs to temporary SQLite."""

    def __init__(self, *, spill_threshold: int = DEFAULT_SPILL_THRESHOLD) -> None:
        if spill_threshold < 1:
            raise ValueError("spill_threshold must be positive")
        self._spill_threshold = spill_threshold
        self._buffer: list[ChartMarker] = []
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None
        self._did_spill = False

    @property
    def did_spill(self) -> bool:
        return self._did_spill

    def add(self, marker: ChartMarker) -> None:
        self._buffer.append(marker)
        if self._connection is None and len(self._buffer) > self._spill_threshold:
            self._start_spill()
        elif self._connection is not None and len(self._buffer) >= SPILL_INSERT_BATCH:
            self._flush()

    def ordered(self) -> Iterable[ChartMarker]:
        connection = self._connection
        if connection is None:
            self._buffer.sort(key=chart_marker_order_key)
            return iter(self._buffer)
        self._flush()
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
        return (chart_marker_from_row(row) for row in cursor)

    def clear(self) -> None:
        self._buffer.clear()

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
        directory = getattr(self, "_directory", None)
        if directory is not None:
            directory.cleanup()
            self._directory = None

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
        self._directory = directory
        self._connection = connection
        self._did_spill = True
        self._flush()

    def _flush(self) -> None:
        connection = self._connection
        if connection is None or not self._buffer:
            return
        connection.executemany(
            "INSERT INTO chart_markers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chart_marker_row(marker) for marker in self._buffer),
        )
        self._buffer.clear()

    def __del__(self) -> None:
        self.close()
