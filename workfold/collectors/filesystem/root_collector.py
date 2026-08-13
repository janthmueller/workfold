"""Collect one prepared filesystem root under a resolved ignore policy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic
from workfold.collectors.filesystem.accounting import AccountingBuilder
from workfold.collectors.filesystem.git_inventory import collect_git_inventory_stream
from workfold.collectors.filesystem.helpers import (
    entry_is_in_scope,
    entry_type,
    ignore_capability,
    ignore_diagnostic,
    retain_entry,
)
from workfold.collectors.filesystem.helpers import origin as build_origin
from workfold.collectors.filesystem.models import CollectedFilesystemEntry
from workfold.collectors.filesystem.timestamps import extract_entry
from workfold.collectors.filesystem.traversal import discover_entries
from workfold.collectors.filesystem.types import (
    FilesystemObservationConsumer,
    LstatReader,
    PendingEntry,
    RootSnapshot,
    ScandirReader,
)
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.collectors.ignores import (
    ExplicitExcluder,
    GitFilesystemInventoryView,
    GitIgnoreProbe,
    GitIgnoreService,
    IgnoreCandidate,
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.coverage import Capability, RecordDisposition
from workfold.models import EntryType, TimestampKind, TimestampObservation
from workfold.scope import ObservationScope


def collect_root(
    root_snapshot: RootSnapshot,
    *,
    kinds: tuple[TimestampKind, ...],
    include_regular_files: bool,
    include_directories: bool,
    include_symlinks: bool,
    respect_gitignore: bool,
    excluder: ExplicitExcluder,
    accounting: AccountingBuilder,
    entries: list[CollectedFilesystemEntry] | None,
    observations: list[TimestampObservation] | None,
    capabilities: list[Capability],
    diagnostics: list[CollectorDiagnostic],
    observation_consumer: FilesystemObservationConsumer | None,
    observation_scope: ObservationScope | None,
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None],
    timestamp_adapter: FilesystemTimestampAdapter,
    ignore_service: GitIgnoreService,
    lstat_reader: LstatReader,
    scandir_reader: ScandirReader,
) -> None:
    """Apply metadata, entry, and ignore semantics to one root."""

    root = root_snapshot.path
    root_type = entry_type(root_snapshot.snapshot.st_mode)

    def consume_eligible(item: PendingEntry) -> None:
        extract_entry(
            item,
            kinds,
            adapter=timestamp_adapter,
            accounting=accounting,
            observations=observations,
            diagnostics=diagnostics,
            observation_consumer=observation_consumer,
            observation_scope=observation_scope,
        )

    if has_git_admin_ancestor(root) or (root_type is EntryType.DIRECTORY and looks_like_bare_repository(root)):
        accounting.discover(root)
        origin = build_origin(root, root, root_type)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
        capabilities.append(
            ignore_capability(
                root,
                respect_gitignore,
                GitIgnoreProbe(None, True, "raw Git administrative storage is semantically excluded"),
                error=None,
            )
        )
        return
    probe = ignore_service.probe(root, is_directory=root_type is EntryType.DIRECTORY)
    if probe.repository is not None and (probe.repository.is_bare or is_within_git_admin(root, probe.repository)):
        accounting.discover(root)
        origin = build_origin(root, root, root_type)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
        capabilities.append(ignore_capability(root, respect_gitignore, probe, error=None))
        return

    def process_visible_entry(item: PendingEntry) -> None:
        if item.entry_type is EntryType.DIRECTORY and not include_directories:
            return
        disposition = (
            RecordDisposition.ELIGIBLE
            if entry_is_in_scope(
                item.entry_type,
                include_regular_files=include_regular_files,
                include_directories=include_directories,
                include_symlinks=include_symlinks,
            )
            else RecordDisposition.EXCLUDED_ENTRY_TYPE
        )
        accounting.discover(root)
        accounting.record(root, disposition)
        retain_entry(entries, item.origin, disposition)
        if disposition is RecordDisposition.ELIGIBLE:
            consume_eligible(item)

    if respect_gitignore and probe.repository is not None and root_type is EntryType.DIRECTORY:
        if not include_directories:
            visit = collect_git_inventory_stream(
                root_snapshot,
                repository=probe.repository,
                include_regular_files=include_regular_files,
                include_symlinks=include_symlinks,
                excluder=excluder,
                accounting=accounting,
                entries=entries,
                diagnostics=diagnostics,
                ignore_service=ignore_service,
                lstat_reader=lstat_reader,
                eligible_consumer=consume_eligible,
                nested_repository_consumer=nested_repository_consumer,
            )
            if visit.error is None:
                capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.warning))
                return
            if ignore_service.transactional_inventory:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(ignore_diagnostic(root, visit.error, warning=False))
                capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.error))
                return
        else:

            def traverse_with_inventory(inventory: GitFilesystemInventoryView) -> None:
                pending = discover_entries(
                    root_snapshot,
                    scandir_reader=scandir_reader,
                    excluder=excluder,
                    accounting=accounting,
                    entries=entries,
                    diagnostics=diagnostics,
                    repository=probe.repository,
                    inventory=inventory,
                    pending_consumer=process_visible_entry,
                    nested_repository_consumer=nested_repository_consumer,
                    include_directories=True,
                )
                if pending:
                    raise RuntimeError("streamed directory traversal unexpectedly retained entries")

            def account_unseen_ignored(relative_path: str, is_directory: bool) -> None:
                disposition = (
                    RecordDisposition.EXPLICITLY_EXCLUDED
                    if excluder.matches(relative_path, is_directory=is_directory)
                    else RecordDisposition.IGNORED
                )
                accounting.discover(root)
                accounting.record(root, disposition)

            visit = ignore_service.inspect_inventory(
                probe.repository,
                root,
                inventory_consumer=traverse_with_inventory,
                unseen_ignored_consumer=account_unseen_ignored,
            )
            if visit.error is None:
                if visit.warning is not None:
                    accounting.discover(root)
                    accounting.record(root, RecordDisposition.RECORD_ERROR)
                    diagnostics.append(ignore_diagnostic(root, visit.warning, warning=False))
                capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.warning))
                return
            if ignore_service.transactional_inventory:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(ignore_diagnostic(root, visit.error, warning=False))
                capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.error))
                return

    defer_ignore_evaluation = respect_gitignore and probe.repository is not None
    pending = discover_entries(
        root_snapshot,
        scandir_reader=scandir_reader,
        excluder=excluder,
        accounting=accounting,
        entries=entries,
        diagnostics=diagnostics,
        repository=probe.repository,
        inventory=None,
        pending_consumer=None if defer_ignore_evaluation else process_visible_entry,
        nested_repository_consumer=nested_repository_consumer,
        include_directories=include_directories,
    )
    ignored: frozenset[Path] = frozenset()
    ignore_error = None
    if respect_gitignore and probe.repository is not None:
        matches = ignore_service.ignored(
            probe.repository,
            tuple(IgnoreCandidate(item.path, item.is_directory) for item in pending),
            lexical_root=root,
        )
        ignored = matches.ignored_paths
        ignore_error = matches.error
        if ignore_error is not None:
            diagnostics.append(ignore_diagnostic(root, ignore_error, warning=False))
    elif respect_gitignore and probe.error is not None:
        ignore_error = probe.error
        visible_repository = has_repository_marker_ancestor(root if root_type is EntryType.DIRECTORY else root.parent)
        diagnostics.append(
            ignore_diagnostic(
                root,
                probe.error,
                warning=probe.error.unavailable and not visible_repository,
            )
        )
    capabilities.append(ignore_capability(root, respect_gitignore, probe, error=ignore_error))

    for item in pending:
        if item.path not in ignored:
            process_visible_entry(item)
            continue
        if item.entry_type is EntryType.DIRECTORY and not include_directories:
            continue
        accounting.discover(root)
        accounting.record(root, RecordDisposition.IGNORED)
        retain_entry(entries, item.origin, RecordDisposition.IGNORED)


__all__ = ["collect_root"]
