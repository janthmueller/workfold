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
    traversal_diagnostic,
)
from workfold.collection.filesystem.ignore import (
    ExplicitExcluder,
    GitFilesystemInventoryVisit,
    GitIgnoreRepository,
    GitIgnoreService,
    is_nested_repository_boundary,
)
from workfold.collection.filesystem.inventory_metadata import AnchoredInventoryMetadata
from workfold.collection.filesystem.linux import LinuxStatxReader
from workfold.collection.filesystem.models import CollectedFilesystemEntry
from workfold.collection.filesystem.root_schedule import RootOwnershipScope
from workfold.collection.filesystem.scan import DirectorySafetyError, PendingEntry, RootSnapshot
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
    statx_reader: LinuxStatxReader | None,
    root_validator: Callable[[], None],
    ownership: RootOwnershipScope,
    eligible_consumer: Callable[[PendingEntry], None],
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None],
) -> GitFilesystemInventoryVisit:
    """Consume the default Git inventory through its bounded disk spool."""

    with AnchoredInventoryMetadata(
        root_snapshot,
        statx_reader=statx_reader,
    ) as metadata:
        return _collect_git_inventory_stream(
            root_snapshot,
            repository=repository,
            include_regular_files=include_regular_files,
            include_symlinks=include_symlinks,
            excluder=excluder,
            accounting=accounting,
            entries=entries,
            diagnostics=diagnostics,
            ignore_service=ignore_service,
            metadata=metadata,
            root_validator=root_validator,
            ownership=ownership,
            eligible_consumer=eligible_consumer,
            nested_repository_consumer=nested_repository_consumer,
        )


def _collect_git_inventory_stream(
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
    metadata: AnchoredInventoryMetadata,
    root_validator: Callable[[], None],
    ownership: RootOwnershipScope,
    eligible_consumer: Callable[[PendingEntry], None],
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None],
) -> GitFilesystemInventoryVisit:
    root = root_snapshot.path

    def consume_included(relative_path: str) -> None:
        relative = PurePosixPath(relative_path)
        path = root if not relative.parts else root / relative_path
        if ownership.delegates(relative):
            return
        try:
            snapshot = metadata.read(relative, display_path=path)
        except (FileNotFoundError, NotADirectoryError):
            return
        except DirectorySafetyError as error:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(traversal_diagnostic(root, path, error))
            return
        except OSError as error:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(stat_diagnostic(root, path, error, is_root=False))
            return

        candidate_type = entry_type(snapshot.st_mode)
        candidate_origin = origin(root, path, candidate_type) if entries is not None else None
        semantic_git_admin = (
            bool(relative.parts)
            and relative.parts[-1] == ".git"
            and is_semantic_git_admin(
                path,
                repository,
            )
        )
        if semantic_git_admin:
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
        relative = PurePosixPath(relative_path)
        if ownership.delegates(relative):
            return
        disposition = (
            RecordDisposition.EXPLICITLY_EXCLUDED
            if excluder.matches(relative_path, is_directory=is_directory)
            else RecordDisposition.IGNORED
        )
        accounting.discover(root)
        accounting.record(root, disposition)

    root_validator()
    visit = ignore_service.visit_inventory(
        repository,
        root,
        included_consumer=consume_included,
        ignored_consumer=consume_ignored,
    )
    root_validator()
    if visit.error is not None:
        return visit

    try:
        selected_is_worktree_root = root.resolve(strict=True) == repository.root.resolve(strict=True)
    except (OSError, RuntimeError):
        selected_is_worktree_root = False
    root_validator()
    if selected_is_worktree_root:
        admin_path = root / ".git"
        try:
            admin_snapshot = metadata.read(PurePosixPath(".git"), display_path=admin_path)
        except FileNotFoundError:
            pass
        except DirectorySafetyError as error:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(traversal_diagnostic(root, admin_path, error))
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
    root_validator()
    return visit
