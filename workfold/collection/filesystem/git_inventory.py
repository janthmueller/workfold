"""Bridge Git's path inventory to current filesystem metadata evidence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath

from workfold.collection.diagnostics import CollectorDiagnostic
from workfold.collection.filesystem.accounting import AccountingBuilder
from workfold.collection.filesystem.entries import (
    entry_is_in_scope,
    entry_type,
    ignore_diagnostic,
    is_semantic_git_admin,
    origin,
    retain_entry,
    stat_diagnostic,
)
from workfold.collection.filesystem.ignore import (
    ExplicitExcluder,
    GitFilesystemInventoryVisit,
    GitIgnoreRepository,
    GitIgnoreService,
    is_nested_repository_boundary,
)
from workfold.collection.filesystem.models import CollectedFilesystemEntry
from workfold.collection.filesystem.scan import LstatReader, PendingEntry, RootSnapshot
from workfold.domain.coverage import RecordDisposition
from workfold.domain.observations import EntryType


def collect_git_inventory_stream(
    root_snapshot: RootSnapshot,
    *,
    repository: GitIgnoreRepository,
    include_regular_files: bool,
    include_symlinks: bool,
    excluder: ExplicitExcluder,
    accounting: AccountingBuilder,
    entries: list[CollectedFilesystemEntry] | None,
    diagnostics: list[CollectorDiagnostic],
    ignore_service: GitIgnoreService,
    lstat_reader: LstatReader,
    eligible_consumer: Callable[[PendingEntry], None],
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None],
) -> GitFilesystemInventoryVisit:
    """Consume the default Git inventory through its bounded disk spool."""

    root = root_snapshot.path

    def consume_included(relative_path: str) -> None:
        path = root if relative_path == "." else root / relative_path
        try:
            snapshot = lstat_reader(path)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as error:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(stat_diagnostic(root, path, error, is_root=False))
            return

        candidate_type = entry_type(snapshot.st_mode)
        candidate_origin = origin(root, path, candidate_type) if entries is not None else None
        relative = PurePosixPath(relative_path)
        if is_semantic_git_admin(path, repository):
            disposition = RecordDisposition.SEMANTIC_GIT_ADMIN
        elif excluder.matches(relative, is_directory=candidate_type is EntryType.DIRECTORY):
            disposition = RecordDisposition.EXPLICITLY_EXCLUDED
        elif candidate_type is EntryType.DIRECTORY and is_nested_repository_boundary(path, selected_root=root):
            nested_repository_consumer(RootSnapshot(path, snapshot), excluder.scoped(relative))
            return
        elif entry_is_in_scope(
            candidate_type,
            include_regular_files=include_regular_files,
            include_directories=False,
            include_symlinks=include_symlinks,
        ):
            disposition = RecordDisposition.ELIGIBLE
        else:
            disposition = RecordDisposition.EXCLUDED_ENTRY_TYPE
        # A nested repository owns its root and descendants. Only records that
        # remain in the parent partition are discovered by that partition.
        accounting.discover(root)
        accounting.record(root, disposition)
        if candidate_origin is not None:
            retain_entry(entries, candidate_origin, disposition)
        if disposition is RecordDisposition.ELIGIBLE:
            eligible_consumer(PendingEntry(root, path, snapshot, candidate_origin, candidate_type))

    def consume_ignored(relative_path: str, is_directory: bool) -> None:
        disposition = (
            RecordDisposition.EXPLICITLY_EXCLUDED
            if excluder.matches(relative_path, is_directory=is_directory)
            else RecordDisposition.IGNORED
        )
        accounting.discover(root)
        accounting.record(root, disposition)

    visit = ignore_service.visit_inventory(
        repository,
        root,
        included_consumer=consume_included,
        ignored_consumer=consume_ignored,
    )
    if visit.error is not None:
        return visit

    try:
        selected_is_worktree_root = root.resolve(strict=True) == repository.root.resolve(strict=True)
    except (OSError, RuntimeError):
        selected_is_worktree_root = False
    if selected_is_worktree_root:
        admin_path = root / ".git"
        try:
            admin_snapshot = lstat_reader(admin_path)
        except FileNotFoundError:
            pass
        except OSError as error:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(stat_diagnostic(root, admin_path, error, is_root=False))
        else:
            admin_type = entry_type(admin_snapshot.st_mode)
            accounting.discover(root)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            if entries is not None:
                admin_origin = origin(root, admin_path, admin_type)
                retain_entry(entries, admin_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)

    if visit.warning is not None:
        accounting.discover(root)
        accounting.record(root, RecordDisposition.RECORD_ERROR)
        diagnostics.append(ignore_diagnostic(root, visit.warning, warning=False))
    return visit
