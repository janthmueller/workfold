"""Collect one prepared filesystem root under a resolved ignore policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from workfold.collection.diagnostics import CollectorDiagnostic
from workfold.collection.filesystem.accounting import AccountingBuilder
from workfold.collection.filesystem.entries import (
    entry_is_in_scope,
    entry_type,
    ignore_capability,
    ignore_diagnostic,
    pending_origin,
    retain_entry,
)
from workfold.collection.filesystem.entries import origin as build_origin
from workfold.collection.filesystem.git_inventory import collect_git_inventory_stream
from workfold.collection.filesystem.ignore import (
    ExplicitExcluder,
    GitFilesystemInventoryView,
    GitIgnoreProbe,
    GitIgnoreService,
    IgnoreCandidate,
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_nested_repository_boundary,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.collection.filesystem.linux import LinuxStatxReader
from workfold.collection.filesystem.metadata import FilesystemTimestampAdapter
from workfold.collection.filesystem.models import CollectedFilesystemEntry
from workfold.collection.filesystem.root_schedule import RootOwnershipScope
from workfold.collection.filesystem.scan import (
    FilesystemObservationConsumer,
    LstatReader,
    PendingEntry,
    RootSnapshot,
    ScandirReader,
    revalidate_directory_snapshot,
)
from workfold.collection.filesystem.timestamps import extract_entry
from workfold.collection.filesystem.traversal import discover_entries
from workfold.domain.coverage import Capability, CapabilityReason, RecordDisposition
from workfold.domain.observations import EntryType, TimestampKind, TimestampObservation
from workfold.domain.scope import ObservationScope


@dataclass(frozen=True, slots=True)
class RootScanRequest:
    """Resolved policy for one prepared filesystem root."""

    entry_timestamps: Mapping[EntryType, tuple[TimestampKind, ...]]
    respect_gitignore: bool
    excluder: ExplicitExcluder
    observation_scope: ObservationScope | None
    ownership: RootOwnershipScope


@dataclass(frozen=True, slots=True)
class RootScanSinks:
    """Mutable accounting and bounded-delivery sinks for one root scan."""

    accounting: AccountingBuilder
    entries: list[CollectedFilesystemEntry] | None
    observations: list[TimestampObservation] | None
    capabilities: list[Capability]
    diagnostics: list[CollectorDiagnostic]
    observation_consumer: FilesystemObservationConsumer | None
    nested_repository_consumer: Callable[[RootSnapshot, ExplicitExcluder], None]


@dataclass(frozen=True, slots=True)
class RootScanServices:
    """Filesystem mechanisms shared by every root in one collection pass."""

    timestamp_adapter: FilesystemTimestampAdapter
    ignore_service: GitIgnoreService
    lstat_reader: LstatReader
    root_identity_reader: LstatReader
    scandir_reader: ScandirReader
    fast_inventory_supported: bool
    inventory_statx_reader: LinuxStatxReader | None


def collect_root(
    root_snapshot: RootSnapshot,
    *,
    request: RootScanRequest,
    sinks: RootScanSinks,
    services: RootScanServices,
) -> None:
    """Apply metadata, entry, and ignore semantics to one root."""

    entry_timestamps = request.entry_timestamps
    respect_gitignore = request.respect_gitignore
    excluder = request.excluder
    observation_scope = request.observation_scope
    ownership = request.ownership
    accounting = sinks.accounting
    entries = sinks.entries
    observations = sinks.observations
    capabilities = sinks.capabilities
    diagnostics = sinks.diagnostics
    observation_consumer = sinks.observation_consumer
    nested_repository_consumer = sinks.nested_repository_consumer
    timestamp_adapter = services.timestamp_adapter
    ignore_service = services.ignore_service
    lstat_reader = services.lstat_reader
    root_identity_reader = services.root_identity_reader
    scandir_reader = services.scandir_reader
    fast_inventory_supported = services.fast_inventory_supported
    inventory_statx_reader = services.inventory_statx_reader
    root = root_snapshot.path
    root_type = entry_type(root_snapshot.snapshot.st_mode)
    include_regular_files = EntryType.REGULAR_FILE in entry_timestamps
    include_directories = EntryType.DIRECTORY in entry_timestamps
    include_symlinks = EntryType.SYMLINK in entry_timestamps

    def validate_root() -> None:
        if root_type is EntryType.DIRECTORY:
            # Root identity checks need only portable no-follow inode metadata;
            # do not repeat an optional statx birth-time request at every
            # orchestration boundary.
            revalidate_directory_snapshot(root, root_snapshot.snapshot, root_identity_reader)

    def consume_eligible(item: PendingEntry) -> None:
        if item.entry_type is None:
            raise RuntimeError("eligible filesystem entry has no supported entry type")
        kinds = entry_timestamps.get(item.entry_type)
        if kinds is None:
            raise RuntimeError("eligible filesystem entry has no timestamp selection")
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

    validate_root()
    semantic_git_admin = has_git_admin_ancestor(root) or (
        root_type is EntryType.DIRECTORY and looks_like_bare_repository(root)
    )
    validate_root()
    if semantic_git_admin:
        accounting.discover(root)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        if entries is not None:
            retain_entry(entries, build_origin(root, root, root_type), RecordDisposition.SEMANTIC_GIT_ADMIN)
        capabilities.append(
            ignore_capability(
                root,
                respect_gitignore,
                GitIgnoreProbe(
                    None,
                    True,
                    "raw Git administrative storage is semantically excluded",
                    capability_reason=CapabilityReason.SEMANTIC_GIT_ADMIN,
                ),
                error=None,
            )
        )
        return
    probe = ignore_service.probe(root, is_directory=root_type is EntryType.DIRECTORY)
    validate_root()
    if probe.repository is not None and (probe.repository.is_bare or is_within_git_admin(root, probe.repository)):
        accounting.discover(root)
        accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
        if entries is not None:
            retain_entry(entries, build_origin(root, root, root_type), RecordDisposition.SEMANTIC_GIT_ADMIN)
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
        if entries is not None:
            retain_entry(entries, pending_origin(item), disposition)
        if disposition is RecordDisposition.ELIGIBLE:
            consume_eligible(item)

    if respect_gitignore and probe.repository is not None and root_type is EntryType.DIRECTORY:
        if not include_directories and fast_inventory_supported:
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
                statx_reader=inventory_statx_reader,
                root_validator=validate_root,
                ownership=ownership,
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
                    lstat_reader=lstat_reader,
                    directory_identity_reader=root_identity_reader,
                    scandir_reader=scandir_reader,
                    excluder=excluder,
                    accounting=accounting,
                    entries=entries,
                    diagnostics=diagnostics,
                    repository=probe.repository,
                    inventory=inventory,
                    pending_consumer=process_visible_entry,
                    nested_repository_consumer=nested_repository_consumer,
                    ownership=ownership,
                    include_directories=include_directories,
                )
                if pending:
                    raise RuntimeError("streamed directory traversal unexpectedly retained entries")

            def account_unseen_ignored(relative_path: str, is_directory: bool) -> None:
                if ownership.delegates(PurePosixPath(relative_path)):
                    return
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
        lstat_reader=lstat_reader,
        directory_identity_reader=root_identity_reader,
        scandir_reader=scandir_reader,
        excluder=excluder,
        accounting=accounting,
        entries=entries,
        diagnostics=diagnostics,
        repository=probe.repository,
        inventory=None,
        pending_consumer=None if defer_ignore_evaluation else process_visible_entry,
        nested_repository_consumer=nested_repository_consumer,
        ownership=ownership,
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
        nested_boundary = item.entry_type is EntryType.DIRECTORY and is_nested_repository_boundary(
            item.path,
            selected_root=root,
        )
        if item.path not in ignored:
            if nested_boundary:
                relative = PurePosixPath(item.path.relative_to(root).as_posix())
                nested_repository_consumer(
                    RootSnapshot(item.path, item.snapshot),
                    excluder.scoped(relative),
                )
                continue
            process_visible_entry(item)
            continue
        if item.entry_type is EntryType.DIRECTORY and not include_directories:
            if nested_boundary:
                # The boundary replaced an otherwise enumerable ignored leaf
                # inventory. Account for it without turning an unrequested
                # directory into retained filesystem evidence.
                accounting.discover(root)
                accounting.record(root, RecordDisposition.IGNORED)
            continue
        if nested_boundary:
            accounting.prune_ignored_subtree()
        accounting.discover(root)
        accounting.record(root, RecordDisposition.IGNORED)
        if entries is not None:
            retain_entry(entries, pending_origin(item), RecordDisposition.IGNORED)


__all__ = ["RootScanRequest", "RootScanServices", "RootScanSinks", "collect_root"]
