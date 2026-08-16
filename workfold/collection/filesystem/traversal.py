"""No-follow native filesystem traversal."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from workfold.collection.diagnostics import DiagnosticSink
from workfold.collection.filesystem.accounting import AccountingBuilder
from workfold.collection.filesystem.entries import (
    entry_type,
    is_semantic_git_admin,
    origin,
    repository_admin_relative_parts,
    retain_entry,
    stat_diagnostic,
    traversal_diagnostic,
)
from workfold.collection.filesystem.ignore import (
    ExplicitExcluder,
    GitFilesystemInventoryView,
    GitIgnoreRepository,
    is_nested_repository_boundary,
)
from workfold.collection.filesystem.ignore.exclusions import filtered_inventory_disposition
from workfold.collection.filesystem.linux import LinuxStatxReader
from workfold.collection.filesystem.models import CollectedFilesystemEntry
from workfold.collection.filesystem.root_schedule import RootOwnershipScope
from workfold.collection.filesystem.scan import (
    DIRECTORY_PUBLICATION_BATCH_SIZE,
    DirectoryEntry,
    DirectorySafetyError,
    LstatReader,
    PendingEntry,
    RootSnapshot,
    ScandirReader,
    StatSnapshot,
    ValidatedBatchPublisher,
    directory_identity,
    revalidate_directory_snapshot,
    statx_fallback_snapshot,
    validate_directory_snapshot_identity,
)
from workfold.domain.coverage import RecordDisposition
from workfold.domain.observations import EntryType


@contextmanager
def scandir_no_follow(
    path: Path,
    expected_snapshot: StatSnapshot,
    *,
    statx_reader: LinuxStatxReader | None = None,
) -> Generator[Iterator[DirectoryEntry], None, None]:
    """Open exactly the queued directory without following replacements."""

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
            opened_snapshot = os.fstat(descriptor)
            validate_directory_snapshot_identity(path, opened_snapshot, expected_snapshot)
            with os.scandir(descriptor) as iterator:
                if statx_reader is None:
                    yield iterator
                else:
                    yield (_StatxDirectoryEntry(entry, descriptor, path, statx_reader) for entry in iterator)
            validate_directory_snapshot_identity(path, _revalidated_directory(path), opened_snapshot)
        finally:
            os.close(descriptor)
        return

    before = _revalidated_directory(path)
    validate_directory_snapshot_identity(path, before, expected_snapshot)
    iterator = os.scandir(path)
    try:
        validate_directory_snapshot_identity(path, _revalidated_directory(path), before)
        with iterator:
            yield iterator
        validate_directory_snapshot_identity(path, _revalidated_directory(path), before)
    finally:
        iterator.close()


def _revalidated_directory(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise DirectorySafetyError(
            error.errno or errno.EIO,
            "queued directory could not be revalidated during traversal",
            os.fspath(path),
        ) from error


@dataclass(frozen=True, slots=True)
class _StatxDirectoryEntry:
    """Read child metadata relative to the same safely opened directory."""

    entry: os.DirEntry[str]
    directory_fd: int
    parent: Path
    reader: LinuxStatxReader

    @property
    def name(self) -> str:
        return self.entry.name

    def stat(self, *, follow_symlinks: bool = True) -> StatSnapshot:
        if follow_symlinks:
            return self.entry.stat(follow_symlinks=True)
        if self.reader.available:
            try:
                return self.reader.read_snapshot_at(
                    self.directory_fd,
                    self.name,
                    display_path=self.parent / self.name,
                )
            except OSError as error:
                return statx_fallback_snapshot(self.entry.stat(follow_symlinks=False), error)
        return self.entry.stat(follow_symlinks=False)


@dataclass(frozen=True, slots=True)
class _DirectoryRead:
    """One child outcome retained until its parent mapping is revalidated."""

    path: Path
    relative: PurePosixPath
    snapshot: StatSnapshot | None = None
    error: OSError | None = None

    def __post_init__(self) -> None:
        if (self.snapshot is None) == (self.error is None):
            raise ValueError("a directory read must contain exactly one snapshot or error")


def discover_entries(
    root_snapshot: RootSnapshot,
    *,
    lstat_reader: LstatReader,
    directory_identity_reader: LstatReader,
    scandir_reader: ScandirReader,
    excluder: ExplicitExcluder,
    accounting: AccountingBuilder,
    entries: list[CollectedFilesystemEntry] | None,
    diagnostics: DiagnosticSink,
    repository: GitIgnoreRepository | None,
    inventory: GitFilesystemInventoryView | None,
    pending_consumer: Callable[[PendingEntry], None] | None,
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None],
    ownership: RootOwnershipScope,
    include_directories: bool,
) -> list[PendingEntry]:
    """Discover current entries lexically without following symlink targets."""

    root = root_snapshot.path
    pending: list[PendingEntry] = []
    root_type = entry_type(root_snapshot.snapshot.st_mode)
    root_origin = origin(root, root, root_type) if entries is not None else None
    root_explicit = root_type is not EntryType.DIRECTORY and excluder.matches(
        PurePosixPath(root.name),
        is_directory=False,
    )
    if is_semantic_git_admin(root, repository):
        accounting.discover(root)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        if root_origin is not None:
            retain_entry(entries, root_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
        return pending
    if root_explicit:
        accounting.discover(root)
        accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
        if root_origin is not None:
            retain_entry(entries, root_origin, RecordDisposition.EXPLICITLY_EXCLUDED)
        return pending
    if root_type is EntryType.DIRECTORY:
        revalidate_directory_snapshot(root, root_snapshot.snapshot, directory_identity_reader)
    _queue_or_consume(
        PendingEntry(root, root, root_snapshot.snapshot, root_origin, root_type),
        pending,
        pending_consumer,
    )
    if root_type is not EntryType.DIRECTORY:
        return pending

    admin_relative_parts = repository_admin_relative_parts(root, repository)
    root_relative = PurePosixPath(".")
    directories = [(root, root_relative, root_snapshot.snapshot)]

    def process_read(item: _DirectoryRead) -> None:
        path = item.path
        if item.error is not None:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(stat_diagnostic(root, path, item.error, is_root=False))
            return
        snapshot = item.snapshot
        if snapshot is None:
            raise RuntimeError("validated directory read omitted its metadata snapshot")
        relative = item.relative
        candidate_type = entry_type(snapshot.st_mode)
        candidate_origin = origin(root, path, candidate_type) if entries is not None else None
        relative_text = relative.as_posix()
        if ownership.delegates(relative):
            # Explicit roots partition otherwise overlapping repository scopes
            # and account for their own records. A delegated directory is
            # pruned here.
            return
        inventory_ignored, inventory_directory = (
            inventory.ignore_state(relative_text) if inventory is not None else (False, False)
        )
        if is_semantic_git_admin(
            path,
            repository,
            relative_parts=relative.parts,
            admin_relative_parts=admin_relative_parts,
        ):
            accounting.discover(root)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            if candidate_origin is not None:
                retain_entry(entries, candidate_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
            return
        nested_boundary = candidate_type is EntryType.DIRECTORY and is_nested_repository_boundary(
            path,
            selected_root=root,
        )
        if inventory_ignored or (candidate_type is EntryType.DIRECTORY and inventory_directory):
            if candidate_type is not EntryType.DIRECTORY or include_directories or nested_boundary:
                if candidate_type is EntryType.DIRECTORY and include_directories:
                    accounting.prune_ignored_subtree()
                disposition = filtered_inventory_disposition(
                    excluder,
                    relative,
                    is_directory=candidate_type is EntryType.DIRECTORY,
                )
                accounting.discover(root)
                accounting.record(root, disposition)
                if candidate_type is EntryType.DIRECTORY and include_directories and candidate_origin is not None:
                    retain_entry(entries, candidate_origin, disposition)
            return
        if excluder.matches(relative, is_directory=candidate_type is EntryType.DIRECTORY):
            accounting.discover(root)
            accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
            if candidate_origin is not None:
                retain_entry(entries, candidate_origin, RecordDisposition.EXPLICITLY_EXCLUDED)
            return
        if nested_boundary:
            # A visible nested worktree owns its descendants and applies its
            # own ignore semantics. An outer ignored boundary was handled
            # above and is never entered.
            if pending_consumer is None:
                # The fallback check-ignore path has not decided visibility
                # yet. Retain only the boundary and defer repository handoff
                # with that decision.
                pending.append(PendingEntry(root, path, snapshot, candidate_origin, candidate_type))
            else:
                nested_repository_consumer(RootSnapshot(path, snapshot), excluder.scoped(relative))
            return
        _queue_or_consume(
            PendingEntry(root, path, snapshot, candidate_origin, candidate_type),
            pending,
            pending_consumer,
        )
        if candidate_type is EntryType.DIRECTORY:
            directories.append((path, relative, snapshot))

    while directories:
        directory, directory_relative, expected_snapshot = directories.pop()
        publisher = ValidatedBatchPublisher(
            validator=lambda current=directory, expected=expected_snapshot: revalidate_directory_snapshot(
                current,
                expected,
                directory_identity_reader,
            ),
            consumer=process_read,
            batch_size=DIRECTORY_PUBLICATION_BATCH_SIZE,
        )
        try:
            with scandir_reader(directory, expected_snapshot) as iterator:
                scan = iter(iterator)
                iteration_error: OSError | None = None
                while True:
                    try:
                        directory_entry = next(scan)
                    except StopIteration:
                        break
                    except OSError as error:
                        iteration_error = error
                        break
                    name = directory_entry.name
                    path = directory / name
                    relative = PurePosixPath(name) if directory_relative == root_relative else directory_relative / name
                    try:
                        snapshot = directory_entry.stat(follow_symlinks=False)
                    except OSError as error:
                        publisher.stage(_DirectoryRead(path, relative, error=error))
                    else:
                        candidate_type = entry_type(snapshot.st_mode)
                        if candidate_type is EntryType.DIRECTORY and directory_identity(snapshot) is None:
                            # Windows DirEntry.stat() deliberately leaves device
                            # and inode identity at zero. Refresh directories
                            # through the same path-based API used to revalidate
                            # them before traversal, while retaining the cheap
                            # DirEntry snapshot for files and capable platforms.
                            try:
                                snapshot = lstat_reader(path)
                            except OSError as error:
                                publisher.stage(_DirectoryRead(path, relative, error=error))
                                snapshot = None
                        if snapshot is not None:
                            publisher.stage(_DirectoryRead(path, relative, snapshot=snapshot))
                publisher.flush()
                if iteration_error is not None:
                    diagnostics.append(traversal_diagnostic(root, directory, iteration_error))
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
