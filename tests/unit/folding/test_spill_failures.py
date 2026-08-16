from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import workfold.folding.spill as spill_module
from workfold.domain.observations import Source, Weekday
from workfold.folding.markers import ChartMarker
from workfold.folding.models import NANOSECONDS_PER_SECOND
from workfold.folding.spill import AggregationStorageError, ChartMarkerStore


def _marker(marker_id: str, second: int) -> ChartMarker:
    return ChartMarker(
        marker_id=marker_id,
        occurred_at_utc_ns=second * NANOSECONDS_PER_SECOND,
        time_of_day_ns=second * NANOSECONDS_PER_SECOND,
        weekday=Weekday.MONDAY,
        source=Source.GIT,
        within_schedule=True,
    )


def test_spill_write_failure_is_structured_and_storage_can_still_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WriteFailingConnection:
        closed = False

        def execute(self, _statement: str) -> WriteFailingConnection:
            return self

        def executemany(self, _statement: str, _rows: object) -> None:
            raise sqlite3.OperationalError("disk is full")

        def close(self) -> None:
            self.closed = True

    connection = WriteFailingConnection()

    def connect_with_write_failure(_database: str) -> WriteFailingConnection:
        return connection

    monkeypatch.setattr(spill_module.sqlite3, "connect", connect_with_write_failure)
    store = ChartMarkerStore(spill_threshold=1)

    store.add(_marker("first", 1))
    with pytest.raises(AggregationStorageError, match="write temporary aggregation storage: disk is full"):
        store.add(_marker("second", 2))

    store.close()
    assert connection.closed


def test_spill_iteration_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect

    class ReadFailingCursor:
        def __iter__(self) -> Any:
            raise sqlite3.OperationalError("temporary database became unreadable")

    class ReadFailingConnection:
        def __init__(self, database: str) -> None:
            self._connection = real_connect(database)

        def execute(self, statement: str) -> Any:
            if statement.lstrip().startswith("SELECT"):
                return ReadFailingCursor()
            return self._connection.execute(statement)

        def executemany(self, statement: str, rows: object) -> Any:
            return self._connection.executemany(statement, rows)  # type: ignore[arg-type]

        def commit(self) -> None:
            self._connection.commit()

        def close(self) -> None:
            self._connection.close()

    def connect_with_read_failure(database: str) -> ReadFailingConnection:
        return ReadFailingConnection(database)

    monkeypatch.setattr(spill_module.sqlite3, "connect", connect_with_read_failure)
    store = ChartMarkerStore(spill_threshold=1)
    try:
        store.add(_marker("first", 1))
        store.add(_marker("second", 2))

        with pytest.raises(AggregationStorageError, match="read temporary aggregation storage"):
            tuple(store.ordered())
    finally:
        store.close()


def test_spill_cleanup_failure_is_structured_and_close_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spill_path = tmp_path / "spill"

    class CleanupFailingDirectory:
        def __init__(self, *, prefix: str) -> None:
            assert prefix == "workfold-aggregation-"
            spill_path.mkdir()
            self.name = str(spill_path)

        def cleanup(self) -> None:
            raise OSError("cleanup denied")

    monkeypatch.setattr(spill_module.tempfile, "TemporaryDirectory", CleanupFailingDirectory)
    store = ChartMarkerStore(spill_threshold=1)
    store.add(_marker("first", 1))
    store.add(_marker("second", 2))

    with pytest.raises(AggregationStorageError, match="clean up temporary aggregation storage: cleanup denied"):
        store.close()

    store.close()
