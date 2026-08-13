"""Public orchestration for Git-authoritative current-filesystem inventories.

Streaming reads are fully validated in a temporary SQLite spool before callers
receive paths. The spool is ephemeral implementation storage, not user state.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from workfold.collection.filesystem.ignore.inventory_format import (
    inventory_commands,
    inventory_stderr_error,
    merge_inventory_stderr,
    parse_error,
    parse_inventory_output,
    resolve_scope,
    unavailable_error,
)
from workfold.collection.filesystem.ignore.inventory_spool import (
    SqliteInventoryView,
    ignored_rows,
    open_inventory_spool,
)
from workfold.collection.filesystem.ignore.models import (
    GitFilesystemInventory,
    GitFilesystemInventoryView,
    GitFilesystemInventoryVisit,
    GitIgnoreCommandError,
    GitIgnoreRepository,
)
from workfold.collection.filesystem.ignore.runner import GitIgnoreRunner


def build_inventory(
    runner: GitIgnoreRunner,
    repository: GitIgnoreRepository,
    selected_root: Path,
) -> GitFilesystemInventory:
    """Materialize current leaf candidates using standard Git ignore semantics."""

    if repository.is_bare:
        return GitFilesystemInventory(error=unavailable_error(repository))
    scope = resolve_scope(repository, selected_root)
    if isinstance(scope, GitIgnoreCommandError):
        return GitFilesystemInventory(error=scope)
    physical_repository_root, selected_prefix = scope

    included_arguments, ignored_arguments, ignored_directory_arguments = inventory_commands(selected_prefix)
    try:
        included_result = runner.run(included_arguments, cwd=physical_repository_root)
        ignored_result = runner.run(ignored_arguments, cwd=physical_repository_root)
        ignored_directory_result = runner.run(ignored_directory_arguments, cwd=physical_repository_root)
        included, _ = parse_inventory_output(included_result.stdout, selected_prefix=selected_prefix)
        ignored, ignored_directories = parse_inventory_output(
            ignored_result.stdout,
            selected_prefix=selected_prefix,
        )
        _, ignored_directory_boundaries = parse_inventory_output(
            ignored_directory_result.stdout,
            selected_prefix=selected_prefix,
        )
        stderr = merge_inventory_stderr(
            (included_result.stderr, ignored_result.stderr, ignored_directory_result.stderr)
        )
        warning = inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
        return GitFilesystemInventory(
            included,
            ignored,
            ignored_directories | ignored_directory_boundaries,
            warning=warning,
        )
    except GitIgnoreCommandError as error:
        return GitFilesystemInventory(error=error)
    except ValueError as error:
        return GitFilesystemInventory(error=parse_error(physical_repository_root, error))


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
        return GitFilesystemInventoryVisit(error=unavailable_error(repository))
    scope = resolve_scope(repository, selected_root)
    if isinstance(scope, GitIgnoreCommandError):
        return GitFilesystemInventoryVisit(error=scope)
    physical_repository_root, selected_prefix = scope
    delivering_callbacks = False
    try:
        with open_inventory_spool(runner, physical_repository_root, selected_prefix) as spool:
            delivering_callbacks = True
            for (raw_path,) in spool.connection.execute(
                "SELECT path FROM inventory WHERE category = 0 ORDER BY ordinal"
            ):
                included_consumer(os.fsdecode(raw_path))
            for raw_path, is_directory in ignored_rows(spool.connection):
                ignored_consumer(os.fsdecode(raw_path), bool(is_directory))
    except GitIgnoreCommandError as error:
        return GitFilesystemInventoryVisit(error=error)
    except (OSError, sqlite3.Error, ValueError) as error:
        if delivering_callbacks:
            raise
        return GitFilesystemInventoryVisit(error=parse_error(physical_repository_root, error))

    return GitFilesystemInventoryVisit(
        included_paths=spool.included_count,
        ignored_paths=spool.ignored_count,
        warning=spool.warning,
    )


def inspect_inventory(
    runner: GitIgnoreRunner,
    repository: GitIgnoreRepository,
    selected_root: Path,
    *,
    inventory_consumer: Callable[[GitFilesystemInventoryView], None],
    unseen_ignored_consumer: Callable[[str, bool], None],
) -> GitFilesystemInventoryVisit:
    """Expose disk-backed ignore membership during one native traversal."""

    if repository.is_bare:
        return GitFilesystemInventoryVisit(error=unavailable_error(repository))
    scope = resolve_scope(repository, selected_root)
    if isinstance(scope, GitIgnoreCommandError):
        return GitFilesystemInventoryVisit(error=scope)
    physical_repository_root, selected_prefix = scope
    delivering_callbacks = False
    try:
        with open_inventory_spool(runner, physical_repository_root, selected_prefix) as spool:
            view = SqliteInventoryView(spool.connection)
            delivering_callbacks = True
            inventory_consumer(view)
            for raw_path, is_directory in ignored_rows(spool.connection, unseen_only=True):
                unseen_ignored_consumer(os.fsdecode(raw_path), bool(is_directory))
    except GitIgnoreCommandError as error:
        return GitFilesystemInventoryVisit(error=error)
    except (OSError, sqlite3.Error, ValueError) as error:
        if delivering_callbacks:
            raise
        return GitFilesystemInventoryVisit(error=parse_error(physical_repository_root, error))

    return GitFilesystemInventoryVisit(
        included_paths=spool.included_count,
        ignored_paths=spool.ignored_count,
        warning=spool.warning,
    )
