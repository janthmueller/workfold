"""Validated, temporary disk-backed storage for Git filesystem inventories."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wuf.collection.filesystem.ignore.inventory_format import (
    InventoryStreamDecoder,
    inventory_commands,
    inventory_stderr_error,
    merge_inventory_stderr,
    normalized_inventory_path,
    parse_error,
)
from wuf.collection.filesystem.ignore.models import GitIgnoreCommandError
from wuf.collection.filesystem.ignore.runner import GitIgnoreRunner


@dataclass(frozen=True, slots=True)
class InventorySpool:
    connection: sqlite3.Connection
    included_count: int
    ignored_count: int
    warning: GitIgnoreCommandError | None


class InventoryStorageError(RuntimeError):
    """A local failure in the inventory's ephemeral SQLite storage."""


class SqliteInventoryView:
    """Query and mark a validated inventory without loading its paths into RAM."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def ignore_state(self, relative_path: str) -> tuple[bool, bool]:
        normalized_path = normalized_inventory_path(os.fsencode(relative_path))
        try:
            category, is_directory = self._connection.execute(
                """
                SELECT (SELECT category FROM inventory WHERE normalized_path = ?),
                       EXISTS(SELECT 1 FROM ignored_directories WHERE normalized_path = ?)
                """,
                (normalized_path, normalized_path),
            ).fetchone()
            ignored = category == 1
            if ignored:
                self._connection.execute(
                    "UPDATE inventory SET seen = 1 WHERE normalized_path = ?",
                    (normalized_path,),
                )
        except sqlite3.Error as error:
            raise InventoryStorageError(f"could not query ignore membership: {error}") from error
        return ignored, bool(is_directory)


@contextmanager
def open_inventory_spool(
    runner: GitIgnoreRunner,
    physical_repository_root: Path,
    selected_prefix: Path,
) -> Generator[InventorySpool, None, None]:
    try:
        directory = tempfile.TemporaryDirectory(prefix="wuf-ignore-inventory-")
    except OSError as error:
        raise InventoryStorageError(f"could not create its temporary directory: {error}") from error

    connection: sqlite3.Connection | None = None
    body_failed = False
    try:
        try:
            connection = sqlite3.connect(f"{directory.name}/inventory.sqlite3")
            _initialize_spool(connection)
            included_count, ignored_count, warning = _populate_inventory(
                connection,
                runner,
                physical_repository_root,
                selected_prefix,
            )
        except ValueError as error:
            raise parse_error(physical_repository_root, error) from error
        except (OSError, sqlite3.Error) as error:
            raise InventoryStorageError(f"could not initialize it: {error}") from error

        yield InventorySpool(connection, included_count, ignored_count, warning)
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_error = _cleanup_inventory_storage(connection, directory)
        if cleanup_error is not None and not body_failed:
            raise InventoryStorageError(f"could not clean it up: {cleanup_error}") from cleanup_error


def included_rows(connection: sqlite3.Connection) -> Iterator[tuple[bytes]]:
    try:
        yield from connection.execute("SELECT path FROM inventory WHERE category = 0 ORDER BY ordinal")
    except sqlite3.Error as error:
        raise InventoryStorageError(f"could not read included paths: {error}") from error


def ignored_rows(
    connection: sqlite3.Connection,
    *,
    unseen_only: bool = False,
) -> Iterator[tuple[bytes, int]]:
    unseen_clause = "AND inventory.seen = 0" if unseen_only else ""
    try:
        yield from connection.execute(
            f"""
            SELECT inventory.path, ignored_directories.normalized_path IS NOT NULL
              FROM inventory
              LEFT JOIN ignored_directories USING (normalized_path)
             WHERE category = 1 {unseen_clause}
             ORDER BY ordinal
            """
        )
    except sqlite3.Error as error:
        raise InventoryStorageError(f"could not read ignored paths: {error}") from error


def _cleanup_inventory_storage(
    connection: sqlite3.Connection | None,
    directory: tempfile.TemporaryDirectory[str],
) -> OSError | sqlite3.Error | None:
    first_error: OSError | sqlite3.Error | None = None
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error as error:
            first_error = error
    try:
        directory.cleanup()
    except OSError as error:
        if first_error is None:
            first_error = error
    return first_error


def _populate_inventory(
    connection: sqlite3.Connection,
    runner: GitIgnoreRunner,
    physical_repository_root: Path,
    selected_prefix: Path,
) -> tuple[int, int, GitIgnoreCommandError | None]:
    commands = inventory_commands(selected_prefix)
    stderr_values: list[bytes] = []
    ordinal = 0

    def insert_path(category: int) -> Callable[[bytes, bool], None]:
        def insert(raw_path: bytes, directory_hint: bool) -> None:
            nonlocal ordinal
            normalized_path = normalized_inventory_path(raw_path)
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO inventory (ordinal, path, normalized_path, category)
                VALUES (?, ?, ?, ?)
                """,
                (ordinal, raw_path, normalized_path, category),
            ).rowcount
            if inserted:
                ordinal += 1
            else:
                existing = connection.execute(
                    "SELECT path, category FROM inventory WHERE normalized_path = ?",
                    (normalized_path,),
                ).fetchone()
                if existing != (raw_path, category):
                    raise ValueError(
                        f"Git filesystem inventory contains a duplicate or overlapping path: {os.fsdecode(raw_path)!r}"
                    )
            if category == 1 and directory_hint:
                connection.execute(
                    "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                    (raw_path, normalized_path),
                )

        return insert

    def insert_directory(raw_path: bytes, directory_hint: bool) -> None:
        if directory_hint:
            connection.execute(
                "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                (raw_path, normalized_inventory_path(raw_path)),
            )

    for arguments, consumer in (
        (commands[0], insert_path(0)),
        (commands[1], insert_path(1)),
        (commands[2], insert_directory),
    ):
        decoder = InventoryStreamDecoder(selected_prefix, consumer)
        stderr_values.append(runner.consume_stdout(arguments, cwd=physical_repository_root, consumer=decoder.feed))
        decoder.finish()
    _validate_inventory_relationships(connection)
    connection.commit()

    included_count = int(connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 0").fetchone()[0])
    ignored_count = int(connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 1").fetchone()[0])
    stderr = merge_inventory_stderr(stderr_values)
    warning = inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
    return included_count, ignored_count, warning


def _initialize_spool(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute(
        """
        CREATE TABLE inventory (
            ordinal INTEGER PRIMARY KEY,
            path BLOB NOT NULL,
            normalized_path BLOB NOT NULL UNIQUE,
            category INTEGER NOT NULL CHECK (category IN (0, 1)),
            seen INTEGER NOT NULL DEFAULT 0 CHECK (seen IN (0, 1))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE ignored_directories (
            path BLOB PRIMARY KEY,
            normalized_path BLOB NOT NULL UNIQUE
        )
        """
    )


def _validate_inventory_relationships(connection: sqlite3.Connection) -> None:
    """Reject included leaves at or below a Git-reported ignored directory."""

    included_count = int(connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 0").fetchone()[0])
    directory_count = int(connection.execute("SELECT COUNT(*) FROM ignored_directories").fetchone()[0])
    conflict: tuple[bytes] | None = None
    if directory_count <= included_count:
        for (directory,) in connection.execute("SELECT normalized_path FROM ignored_directories"):
            prefix = directory.rstrip(b"/") + b"/"
            upper_bound = directory.rstrip(b"/") + b"0"
            conflict = connection.execute(
                """
                SELECT path FROM inventory
                 WHERE category = 0
                   AND (normalized_path = ? OR (normalized_path >= ? AND normalized_path < ?))
                 LIMIT 1
                """,
                (directory, prefix, upper_bound),
            ).fetchone()
            if conflict is not None:
                break
    else:
        for raw_path, normalized_path in connection.execute(
            "SELECT path, normalized_path FROM inventory WHERE category = 0"
        ):
            parts = normalized_path.split(b"/")
            for size in range(1, len(parts) + 1):
                candidate = b"/".join(parts[:size])
                if (
                    connection.execute(
                        "SELECT 1 FROM ignored_directories WHERE normalized_path = ?",
                        (candidate,),
                    ).fetchone()
                    is not None
                ):
                    conflict = (raw_path,)
                    break
            if conflict is not None:
                break
    if conflict is not None:
        raise ValueError(
            f"Git filesystem inventory places an included path below an ignored directory: {os.fsdecode(conflict[0])!r}"
        )
