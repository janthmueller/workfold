"""Git-authoritative current-filesystem inventories.

The streaming path validates all Git output in temporary SQLite before it
invokes consumers. SQLite is an ephemeral spool here, not persistent state.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Final

from workfold.collectors.ignores.models import (
    GitFilesystemInventory,
    GitFilesystemInventoryVisit,
    GitIgnoreCommandError,
    GitIgnoreRepository,
)
from workfold.collectors.ignores.runner import GitIgnoreRunner

MAX_INVENTORY_STDERR_BYTES: Final[int] = 16_384
INVENTORY_PATHS_NEED_NORMALIZATION: Final[bool] = os.path.normcase("A/B") != "A/B"


def build_inventory(
    runner: GitIgnoreRunner,
    repository: GitIgnoreRepository,
    selected_root: Path,
) -> GitFilesystemInventory:
    """Materialize current leaf candidates using standard Git ignore semantics."""

    if repository.is_bare:
        return GitFilesystemInventory(error=_unavailable_error(repository))
    scope = _resolve_scope(repository, selected_root)
    if isinstance(scope, GitIgnoreCommandError):
        return GitFilesystemInventory(error=scope)
    physical_repository_root, selected_prefix = scope

    included_arguments, ignored_arguments, ignored_directory_arguments = _inventory_commands(selected_prefix)
    try:
        included_result = runner.run(included_arguments, cwd=physical_repository_root)
        ignored_result = runner.run(ignored_arguments, cwd=physical_repository_root)
        ignored_directory_result = runner.run(ignored_directory_arguments, cwd=physical_repository_root)
        included, _ = _parse_inventory_output(included_result.stdout, selected_prefix=selected_prefix)
        ignored, ignored_directories = _parse_inventory_output(
            ignored_result.stdout,
            selected_prefix=selected_prefix,
        )
        _, ignored_directory_boundaries = _parse_inventory_output(
            ignored_directory_result.stdout,
            selected_prefix=selected_prefix,
        )
        stderr = _merge_inventory_stderr(
            (included_result.stderr, ignored_result.stderr, ignored_directory_result.stderr)
        )
        warning = _inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
        return GitFilesystemInventory(
            included,
            ignored,
            ignored_directories | ignored_directory_boundaries,
            warning=warning,
        )
    except GitIgnoreCommandError as error:
        return GitFilesystemInventory(error=error)
    except ValueError as error:
        return GitFilesystemInventory(error=_parse_error(physical_repository_root, error))


def visit_inventory(
    runner: GitIgnoreRunner,
    repository: GitIgnoreRepository,
    selected_root: Path,
    *,
    included_consumer: Callable[[str], None],
    ignored_consumer: Callable[[str, bool], None],
) -> GitFilesystemInventoryVisit:
    """Visit a complete inventory without retaining every path in RAM."""

    if repository.is_bare:
        return GitFilesystemInventoryVisit(error=_unavailable_error(repository))
    scope = _resolve_scope(repository, selected_root)
    if isinstance(scope, GitIgnoreCommandError):
        return GitFilesystemInventoryVisit(error=scope)
    physical_repository_root, selected_prefix = scope
    commands = _inventory_commands(selected_prefix)

    stderr_values: list[bytes] = []
    delivering_callbacks = False
    try:
        with tempfile.TemporaryDirectory(prefix="workfold-ignore-inventory-") as directory:
            connection = sqlite3.connect(f"{directory}/inventory.sqlite3")
            try:
                _initialize_spool(connection)
                ordinal = 0

                def insert_path(category: int) -> Callable[[bytes, bool], None]:
                    def insert(raw_path: bytes, directory_hint: bool) -> None:
                        nonlocal ordinal
                        normalized_path = _normalized_inventory_path(raw_path)
                        inserted = connection.execute(
                            "INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, ?)",
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
                                    "Git filesystem inventory contains a duplicate or overlapping path: "
                                    f"{os.fsdecode(raw_path)!r}"
                                )
                        if category == 1 and directory_hint:
                            connection.execute(
                                "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                                (raw_path, normalized_path),
                            )

                    return insert

                def insert_directory(raw_path: bytes, _directory_hint: bool) -> None:
                    connection.execute(
                        "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                        (raw_path, _normalized_inventory_path(raw_path)),
                    )

                for arguments, consumer in (
                    (commands[0], insert_path(0)),
                    (commands[1], insert_path(1)),
                    (commands[2], insert_directory),
                ):
                    decoder = InventoryStreamDecoder(selected_prefix, consumer)
                    stderr_values.append(
                        runner.consume_stdout(arguments, cwd=physical_repository_root, consumer=decoder.feed)
                    )
                    decoder.finish()
                _validate_inventory_relationships(connection)
                connection.commit()

                included_count = int(
                    connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 0").fetchone()[0]
                )
                ignored_count = int(
                    connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 1").fetchone()[0]
                )
                delivering_callbacks = True
                for (raw_path,) in connection.execute("SELECT path FROM inventory WHERE category = 0 ORDER BY ordinal"):
                    included_consumer(os.fsdecode(raw_path))
                for raw_path, is_directory in connection.execute(
                    """
                    SELECT inventory.path, ignored_directories.path IS NOT NULL
                      FROM inventory
                      LEFT JOIN ignored_directories USING (path)
                     WHERE category = 1
                     ORDER BY ordinal
                    """
                ):
                    ignored_consumer(os.fsdecode(raw_path), bool(is_directory))
            finally:
                connection.close()
    except GitIgnoreCommandError as error:
        return GitFilesystemInventoryVisit(error=error)
    except (OSError, sqlite3.Error, ValueError) as error:
        if delivering_callbacks:
            raise
        return GitFilesystemInventoryVisit(error=_parse_error(physical_repository_root, error))

    stderr = _merge_inventory_stderr(stderr_values)
    warning = _inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
    return GitFilesystemInventoryVisit(
        included_paths=included_count,
        ignored_paths=ignored_count,
        warning=warning,
    )


class InventoryStreamDecoder:
    """Turn arbitrary stdout chunks into validated NUL-delimited paths."""

    def __init__(self, selected_prefix: Path, consumer: Callable[[bytes, bool], None]) -> None:
        self._prefix_parts = _inventory_prefix_parts(selected_prefix)
        self._consumer = consumer
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer.extend(chunk)
        start = 0
        while True:
            end = self._buffer.find(0, start)
            if end < 0:
                break
            raw_path = bytes(self._buffer[start:end])
            selected_relative, directory_hint = _parse_inventory_record(
                raw_path,
                prefix_parts=self._prefix_parts,
            )
            self._consumer(selected_relative, directory_hint)
            start = end + 1
        if start:
            del self._buffer[:start]

    def finish(self) -> None:
        if self._buffer:
            raise ValueError("NUL-delimited Git output has no final terminator")


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
            category INTEGER NOT NULL CHECK (category IN (0, 1))
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


def _resolve_scope(
    repository: GitIgnoreRepository,
    selected_root: Path,
) -> tuple[Path, Path] | GitIgnoreCommandError:
    try:
        physical_repository_root = repository.root.resolve(strict=True)
        physical_selected_root = selected_root.resolve(strict=True)
        selected_prefix = physical_selected_root.relative_to(physical_repository_root)
    except (OSError, RuntimeError, ValueError) as error:
        return GitIgnoreCommandError(
            code="git_filesystem_inventory_path_mapping_error",
            message=f"could not map the selected filesystem root into the Git worktree: {error}",
            cwd=repository.root,
            command=("ls-files",),
        )
    return physical_repository_root, selected_prefix


def _inventory_commands(selected_prefix: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        _inventory_arguments(("--cached", "--others", "--exclude-standard"), selected_prefix=selected_prefix),
        _inventory_arguments(("--others", "--ignored", "--exclude-standard"), selected_prefix=selected_prefix),
        _inventory_arguments(
            ("--others", "--ignored", "--exclude-standard", "--directory"),
            selected_prefix=selected_prefix,
        ),
    )


def _inventory_arguments(options: tuple[str, ...], *, selected_prefix: Path) -> tuple[str, ...]:
    arguments = ("ls-files", "-z", "--full-name", *options)
    if selected_prefix == Path("."):
        return arguments
    pathspec = f":(top,literal){selected_prefix.as_posix()}"
    return (*arguments, "--", pathspec)


def _normalized_inventory_path(raw_path: bytes) -> bytes:
    if not INVENTORY_PATHS_NEED_NORMALIZATION:
        return raw_path
    return b"/".join(os.fsencode(os.path.normcase(os.fsdecode(part))) for part in raw_path.split(b"/"))


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


def _parse_inventory_output(output: bytes, *, selected_prefix: Path) -> tuple[tuple[str, ...], frozenset[str]]:
    if output and not output.endswith(b"\0"):
        raise ValueError("NUL-delimited Git output has no final terminator")
    raw_paths = output[:-1].split(b"\0") if output else ()
    prefix_parts = _inventory_prefix_parts(selected_prefix)
    paths: list[str] = []
    directory_hints: set[str] = set()
    seen: set[bytes] = set()
    for raw_path in raw_paths:
        selected_relative, directory_hint = _parse_inventory_record(raw_path, prefix_parts=prefix_parts)
        if selected_relative not in seen:
            decoded = os.fsdecode(selected_relative)
            paths.append(decoded)
            seen.add(selected_relative)
        else:
            decoded = os.fsdecode(selected_relative)
        if directory_hint:
            directory_hints.add(decoded)
    return tuple(paths), frozenset(directory_hints)


def _inventory_prefix_parts(selected_prefix: Path) -> tuple[bytes, ...] | None:
    if selected_prefix == Path("."):
        return None
    return tuple(os.fsencode(part) for part in PurePosixPath(selected_prefix.as_posix()).parts)


def _parse_inventory_record(raw_path: bytes, *, prefix_parts: tuple[bytes, ...] | None) -> tuple[bytes, bool]:
    if not raw_path:
        raise ValueError("Git returned an empty inventory path")
    directory_hint = raw_path.endswith(b"/")
    if directory_hint:
        raw_path = raw_path[:-1]
        if not raw_path:
            raise ValueError("Git returned an empty inventory directory")
    parts = raw_path.split(b"/")
    if raw_path.startswith(b"/") or any(part in {b"", b".", b".."} for part in parts):
        raise ValueError(f"Git returned an unsafe inventory path: {os.fsdecode(raw_path)!r}")
    if prefix_parts is None:
        return raw_path, directory_hint
    if len(parts) >= len(prefix_parts) and all(
        os.path.normcase(os.fsdecode(actual)) == os.path.normcase(os.fsdecode(expected))
        for actual, expected in zip(parts[: len(prefix_parts)], prefix_parts, strict=True)
    ):
        remainder = parts[len(prefix_parts) :]
        return (b"/".join(remainder) if remainder else b"."), directory_hint
    raise ValueError(f"Git returned a path outside the selected root: {os.fsdecode(raw_path)!r}")


def _merge_inventory_stderr(values: Sequence[bytes]) -> bytes:
    return b"\n".join(dict.fromkeys(line for output in values for line in output.splitlines() if line))[
        :MAX_INVENTORY_STDERR_BYTES
    ]


def _inventory_stderr_error(root: Path, command: tuple[str, ...], stderr: bytes) -> GitIgnoreCommandError:
    bounded = stderr[:MAX_INVENTORY_STDERR_BYTES]
    lines = bounded.decode("utf-8", errors="surrogateescape").splitlines()
    detail = "; ".join(_without_git_severity_prefix(line) for line in lines)
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_incomplete",
        message=detail,
        cwd=root,
        command=command,
        stderr=bounded,
    )


def _without_git_severity_prefix(message: str) -> str:
    for prefix in ("warning: ", "error: ", "fatal: "):
        if message.casefold().startswith(prefix):
            return message[len(prefix) :]
    return message


def _unavailable_error(repository: GitIgnoreRepository) -> GitIgnoreCommandError:
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_unavailable",
        message="a bare repository has no filesystem worktree inventory",
        cwd=repository.root,
        command=("ls-files",),
    )


def _parse_error(root: Path, error: Exception) -> GitIgnoreCommandError:
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_parse_error",
        message=f"could not parse Git filesystem inventory: {error}",
        cwd=root,
        command=("ls-files",),
    )
