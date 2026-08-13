"""Bounded semantic reflog validation and temporary SQLite spooling."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path

from workfold.collection.git.reflogs.models import (
    GitReflogParseError,
    GitReflogReadError,
    ParsedReflogEntry,
    ReflogVisit,
)
from workfold.collection.git.reflogs.parser import parse_reflog_line
from workfold.collection.git.reflogs.reader import open_semantic_reflog, snapshot_changed
from workfold.collection.git.repository import GitRepository


def visit_semantic_reflog(
    path: Path,
    *,
    repository: GitRepository,
    ref_name: str,
    entry_consumer: Callable[[tuple[ParsedReflogEntry, ...]], None] | None = None,
    batch_size: int = 512,
) -> ReflogVisit:
    """Validate a complete reflog snapshot, then emit newest-first batches."""

    if batch_size < 1:
        raise ValueError("reflog batch_size must be positive")
    descriptor, before, _resolved = open_semantic_reflog(path, repository=repository)
    record_count = 0
    has_nul = False
    truncated = False
    delivering_callbacks = False
    try:
        with tempfile.TemporaryDirectory(prefix="workfold-reflog-") as directory:
            connection = sqlite3.connect(f"{directory}/reflog.sqlite3")
            try:
                _initialize_spool(connection)
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    for raw_record in stream:
                        has_nul |= b"\0" in raw_record
                        if not raw_record.endswith(b"\n"):
                            truncated = True
                            continue
                        connection.execute(
                            "INSERT INTO records VALUES (?, ?, NULL)",
                            (record_count, raw_record[:-1]),
                        )
                        record_count += 1
                    after = os.fstat(stream.fileno())

                if has_nul:
                    raise GitReflogParseError(
                        "invalid_git_reflog_entry",
                        "reflog contains an impossible NUL byte",
                        ref_name=ref_name,
                        record_count=record_count,
                    )
                if truncated:
                    raise GitReflogParseError(
                        "truncated_git_reflog_entry",
                        "reflog ends inside a record",
                        ref_name=ref_name,
                        record_count=record_count,
                    )

                try:
                    _index_duplicate_ordinals(connection, ref_name=ref_name)
                except GitReflogParseError as error:
                    raise GitReflogParseError(
                        error.code,
                        str(error),
                        ref_name=error.ref_name,
                        record_count=record_count,
                    ) from error
                connection.commit()

                raw_ref_name = os.fsencode(ref_name)
                captured = 0
                batch: list[ParsedReflogEntry] = []
                for ordinal, raw_line, duplicate_ordinal in connection.execute(
                    "SELECT ordinal, raw_line, duplicate_ordinal FROM records ORDER BY ordinal DESC"
                ):
                    parsed = parse_reflog_line(raw_line, ref_name=ref_name)
                    batch.append(
                        parsed.to_entry(
                            ref_name=ref_name,
                            raw_ref_name=raw_ref_name,
                            selector_index=record_count - int(ordinal) - 1,
                            duplicate_ordinal=int(duplicate_ordinal),
                        )
                    )
                    if len(batch) >= batch_size:
                        if entry_consumer is not None:
                            delivering_callbacks = True
                            entry_consumer(tuple(batch))
                        captured += len(batch)
                        batch.clear()
                if batch:
                    if entry_consumer is not None:
                        delivering_callbacks = True
                        entry_consumer(tuple(batch))
                    captured += len(batch)
            finally:
                connection.close()
    except GitReflogParseError:
        raise
    except (OSError, sqlite3.Error) as error:
        if delivering_callbacks:
            raise
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return ReflogVisit(
        entry_count=record_count,
        captured_entry_count=captured,
        changed_during_read=snapshot_changed(before, after),
    )


def _initialize_spool(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-4096")
    connection.execute(
        """
        CREATE TABLE records (
            ordinal INTEGER PRIMARY KEY,
            raw_line BLOB NOT NULL,
            duplicate_ordinal INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE duplicate_counts (
            old_id BLOB NOT NULL,
            new_id BLOB NOT NULL,
            identity BLOB NOT NULL,
            raw_timestamp BLOB NOT NULL,
            message BLOB NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (old_id, new_id, identity, raw_timestamp, message)
        ) WITHOUT ROWID
        """
    )


def _index_duplicate_ordinals(connection: sqlite3.Connection, *, ref_name: str) -> None:
    for ordinal, raw_line in connection.execute("SELECT ordinal, raw_line FROM records ORDER BY ordinal"):
        parsed = parse_reflog_line(raw_line, ref_name=ref_name)
        existing = connection.execute(
            """
            SELECT count FROM duplicate_counts
             WHERE old_id = ? AND new_id = ? AND identity = ?
               AND raw_timestamp = ? AND message = ?
            """,
            parsed.duplicate_key,
        ).fetchone()
        duplicate_ordinal = 0 if existing is None else int(existing[0])
        if existing is None:
            connection.execute("INSERT INTO duplicate_counts VALUES (?, ?, ?, ?, ?, 1)", parsed.duplicate_key)
        else:
            connection.execute(
                """
                UPDATE duplicate_counts SET count = count + 1
                 WHERE old_id = ? AND new_id = ? AND identity = ?
                   AND raw_timestamp = ? AND message = ?
                """,
                parsed.duplicate_key,
            )
        connection.execute(
            "UPDATE records SET duplicate_ordinal = ? WHERE ordinal = ?",
            (duplicate_ordinal, ordinal),
        )
