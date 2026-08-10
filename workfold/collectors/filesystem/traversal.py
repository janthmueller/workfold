"""No-follow native filesystem traversal."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from workfold.collectors.base import CollectorDiagnostic
from workfold.collectors.filesystem.accounting import AccountingBuilder
from workfold.collectors.filesystem.helpers import (
    entry_type,
    is_semantic_git_admin,
    origin,
    repository_admin_relative_parts,
    retain_entry,
    stat_diagnostic,
    traversal_diagnostic,
)
from workfold.collectors.filesystem.models import CollectedFilesystemEntry
from workfold.collectors.filesystem.types import DirectorySafetyError, PendingEntry, RootSnapshot, ScandirReader
from workfold.collectors.ignores import (
    ExplicitExcluder,
    GitFilesystemInventory,
    GitIgnoreRepository,
    is_nested_repository_boundary,
)
from workfold.coverage import RecordDisposition
from workfold.models import EntryType


@contextmanager
def scandir_no_follow(path: Path) -> Generator[Iterator[os.DirEntry[str]], None, None]:
    """Open a directory without following a replacement final symlink."""

    supports_descriptor_scan = os.scandir in os.supports_fd
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if supports_descriptor_scan and no_follow and directory_only:
        flags = os.O_RDONLY | no_follow | directory_only | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise DirectorySafetyError(
                    error.errno,
                    "queued directory became a non-directory or symlink before traversal",
                    os.fspath(path),
                ) from error
            raise
        try:
            with os.scandir(descriptor) as iterator:
                yield iterator
        finally:
            os.close(descriptor)
        return

    try:
        before = os.lstat(path)
    except OSError as error:
        raise DirectorySafetyError(
            error.errno,
            "queued directory could not be revalidated before traversal",
            os.fspath(path),
        ) from error
    if not stat.S_ISDIR(before.st_mode):
        raise DirectorySafetyError(
            errno.ENOTDIR,
            "queued directory became a non-directory or symlink before traversal",
            os.fspath(path),
        )
    iterator = os.scandir(path)
    try:
        after = os.lstat(path)
        if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise DirectorySafetyError(
                errno.ENOTDIR,
                "queued directory changed while traversal was opening it",
                os.fspath(path),
            )
        with iterator:
            yield iterator
    finally:
        iterator.close()


def discover_entries(
    root_snapshot: RootSnapshot,
    *,
    scandir_reader: ScandirReader,
    excluder: ExplicitExcluder,
    accounting: AccountingBuilder,
    entries: list[CollectedFilesystemEntry] | None,
    diagnostics: list[CollectorDiagnostic],
    repository: GitIgnoreRepository | None,
    inventory: GitFilesystemInventory | None,
    inventory_ignored_seen: set[str],
    pending_consumer: Callable[[PendingEntry], None] | None,
    include_directories: bool,
) -> list[PendingEntry]:
    """Discover current entries lexically without following symlink targets."""

    root = root_snapshot.path
    pending: list[PendingEntry] = []
    root_type = entry_type(root_snapshot.snapshot.st_mode)
    root_origin = origin(root, root, root_type)
    root_explicit = root_type is not EntryType.DIRECTORY and excluder.matches(
        PurePosixPath(root.name),
        is_directory=False,
    )
    if is_semantic_git_admin(root, repository):
        accounting.discover(root)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        retain_entry(entries, root_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
        return pending
    if root_explicit:
        accounting.discover(root)
        accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
        retain_entry(entries, root_origin, RecordDisposition.EXPLICITLY_EXCLUDED)
        return pending
    _queue_or_consume(
        PendingEntry(root, root, root_snapshot.snapshot, root_origin, root_type),
        pending,
        pending_consumer,
    )
    if root_type is not EntryType.DIRECTORY:
        return pending

    inventory_ignored_paths: set[str] = set(inventory.ignored_relative_paths) if inventory is not None else set()
    inventory_directory_paths: frozenset[str] = (
        inventory.ignored_directory_paths if inventory is not None else frozenset()
    )
    admin_relative_parts = repository_admin_relative_parts(root, repository)
    root_relative = PurePosixPath(".")
    directories = [(root, root_relative)]
    while directories:
        directory, directory_relative = directories.pop()
        try:
            with scandir_reader(directory) as iterator:
                try:
                    for directory_entry in iterator:
                        name = directory_entry.name
                        path = directory / name
                        try:
                            snapshot = directory_entry.stat(follow_symlinks=False)
                        except OSError as error:
                            accounting.discover(root)
                            accounting.record(root, RecordDisposition.RECORD_ERROR)
                            diagnostics.append(stat_diagnostic(root, path, error, is_root=False))
                            continue

                        candidate_type = entry_type(snapshot.st_mode)
                        candidate_origin = origin(root, path, candidate_type)
                        relative = PurePosixPath(name) if directory_relative == root_relative else directory_relative / name
                        relative_text = relative.as_posix()
                        inventory_ignored = relative_text in inventory_ignored_paths
                        if inventory_ignored:
                            inventory_ignored_seen.add(relative_text)
                        if is_semantic_git_admin(
                            path,
                            repository,
                            relative_parts=relative.parts,
                            admin_relative_parts=admin_relative_parts,
                        ):
                            accounting.discover(root)
                            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                            retain_entry(entries, candidate_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
                            continue
                        if excluder.matches(relative, is_directory=candidate_type is EntryType.DIRECTORY):
                            accounting.discover(root)
                            accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
                            retain_entry(entries, candidate_origin, RecordDisposition.EXPLICITLY_EXCLUDED)
                            continue
                        if candidate_type is EntryType.DIRECTORY and is_nested_repository_boundary(
                            path,
                            selected_root=root,
                        ):
                            accounting.discover(root)
                            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                            retain_entry(entries, candidate_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
                            continue
                        if inventory_ignored or (
                            candidate_type is EntryType.DIRECTORY and relative_text in inventory_directory_paths
                        ):
                            if candidate_type is not EntryType.DIRECTORY or include_directories:
                                accounting.discover(root)
                                accounting.record(root, RecordDisposition.IGNORED)
                                retain_entry(entries, candidate_origin, RecordDisposition.IGNORED)
                            continue
                        _queue_or_consume(
                            PendingEntry(root, path, snapshot, candidate_origin, candidate_type),
                            pending,
                            pending_consumer,
                        )
                        if candidate_type is EntryType.DIRECTORY:
                            directories.append((path, relative))
                except OSError as error:
                    diagnostics.append(traversal_diagnostic(root, directory, error))
        except OSError as error:
            diagnostics.append(traversal_diagnostic(root, directory, error))
    return pending


def _queue_or_consume(
    item: PendingEntry,
    pending: list[PendingEntry],
    consumer: Callable[[PendingEntry], None] | None,
) -> None:
    if consumer is None:
        pending.append(item)
    else:
        consumer(item)
