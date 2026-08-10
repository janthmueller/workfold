"""Current-snapshot filesystem timestamp collection.

Git-aware scans use Git only to inventory paths under its authoritative ignore
semantics; a current no-follow stat remains the entry/type/timestamp boundary.
Native traversal is lexical and never follows symbolic-link targets. Every
discovered entry receives one record disposition and every eligible timestamp
slot receives one extraction disposition. Portable timestamps share the
discovery stat snapshot; Linux birth time adds one identity-checked statx read.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.filesystem.accounting import AccountingBuilder
from workfold.collectors.filesystem.git_inventory import collect_git_inventory_stream
from workfold.collectors.filesystem.helpers import (
    account_inventory_ignored,
    account_inventory_warning,
    crosses_nested_repository,
    entry_is_in_scope,
    entry_type,
    ignore_capability,
    ignore_diagnostic,
    is_lexical_descendant,
    retain_entry,
    stat_diagnostic,
)
from workfold.collectors.filesystem.helpers import (
    origin as build_origin,
)
from workfold.collectors.filesystem.models import (
    CollectedFilesystemEntry,
    FilesystemAccounting,
    FilesystemCollectionResult,
    TimestampExtractionCoverage,
)
from workfold.collectors.filesystem.timestamps import extract_entry
from workfold.collectors.filesystem.traversal import discover_entries, scandir_no_follow
from workfold.collectors.filesystem.types import (
    DirectorySafetyError,
    FilesystemObservationConsumer,
    LstatReader,
    PendingEntry,
    RootSnapshot,
    ScandirReader,
    lstat,
)
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.collectors.ignores import (
    ExplicitExcluder,
    GitFilesystemInventory,
    GitIgnoreProbe,
    GitIgnoreService,
    IgnoreCandidate,
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.coverage import (
    Capability,
    RecordDisposition,
)
from workfold.models import EntryType, Source, TimestampKind, TimestampObservation
from workfold.provenance import lexical_absolute


class FilesystemCollector:
    """Collect current metadata observations beneath exact selected roots."""

    def __init__(
        self,
        *,
        timestamp_adapter: FilesystemTimestampAdapter | None = None,
        ignore_service: GitIgnoreService | None = None,
        lstat_reader: LstatReader = lstat,
        scandir_reader: ScandirReader = scandir_no_follow,
    ) -> None:
        self._timestamp_adapter = timestamp_adapter or FilesystemTimestampAdapter()
        self._ignore_service = ignore_service or GitIgnoreService()
        self._lstat = lstat_reader
        self._scandir = scandir_reader

    def collect(
        self,
        paths: Sequence[Path],
        *,
        timestamp_kinds: Sequence[TimestampKind],
        include_regular_files: bool = True,
        include_directories: bool = False,
        include_symlinks: bool = False,
        respect_gitignore: bool = True,
        include_ignored: bool = False,
        exclusions: Sequence[str] = (),
        cwd: Path | None = None,
        observation_consumer: FilesystemObservationConsumer | None = None,
        retain_entries: bool = True,
        retain_observations: bool = True,
    ) -> FilesystemCollectionResult:
        """Collect all requested metadata slots without applying date filters."""

        if not paths:
            raise ValueError("filesystem collection needs at least one path")
        if respect_gitignore == include_ignored:
            raise ValueError("select exactly one filesystem ignore policy")
        kinds = tuple(dict.fromkeys(timestamp_kinds))
        if not kinds:
            raise ValueError("filesystem collection needs at least one timestamp kind")
        if any(kind.source is not Source.FILESYSTEM for kind in kinds):
            raise ValueError("filesystem collector accepts only filesystem timestamp kinds")
        excluder = ExplicitExcluder.compile(exclusions)
        base = lexical_absolute(cwd or Path.cwd())
        requested = tuple(lexical_absolute(path, base=base) for path in paths)

        diagnostics = DiagnosticBuffer()
        roots, scan_roots, overlap_count = self._prepare_roots(requested, diagnostics)
        entries: list[CollectedFilesystemEntry] | None = [] if retain_entries else None
        observations: list[TimestampObservation] | None = [] if retain_observations else None
        capabilities: list[Capability] = []
        accounting = AccountingBuilder(retain_observation_ids=retain_observations)

        for root_snapshot in roots:
            root = root_snapshot.path
            accounting.ensure_root(root, kinds)
            capabilities.extend(self._timestamp_adapter.capability(kind, target=os.fspath(root)) for kind in kinds)
            self._collect_root(
                root_snapshot,
                kinds=kinds,
                include_regular_files=include_regular_files,
                include_directories=include_directories,
                include_symlinks=include_symlinks,
                respect_gitignore=respect_gitignore,
                excluder=excluder,
                accounting=accounting,
                entries=entries,
                observations=observations,
                capabilities=capabilities,
                diagnostics=diagnostics,
                observation_consumer=observation_consumer,
            )

        return FilesystemCollectionResult(
            entries=tuple(entries or ()),
            observations=tuple(observations or ()),
            accounting=accounting.build(),
            capabilities=tuple(capabilities),
            diagnostics=diagnostics.snapshot(),
            requested_roots=requested,
            scan_roots=scan_roots,
            successful_roots=tuple(item.path for item in roots),
            overlapping_roots_deduplicated=overlap_count,
        )

    def _prepare_roots(
        self,
        requested: Sequence[Path],
        diagnostics: list[CollectorDiagnostic],
    ) -> tuple[list[RootSnapshot], tuple[Path, ...], int]:
        indexed: list[tuple[int, Path]] = []
        seen: set[str] = set()
        overlap_count = 0
        for index, path in enumerate(requested):
            key = os.path.normcase(os.fspath(path))
            if key in seen:
                overlap_count += 1
                continue
            seen.add(key)
            indexed.append((index, path))
        indexed.sort(key=lambda item: (len(item[1].parts), item[0]))

        roots: list[RootSnapshot] = []
        scan_roots: list[Path] = []
        covering_directories: list[Path] = []
        for _, path in indexed:
            covering = next((root for root in covering_directories if is_lexical_descendant(path, root)), None)
            if covering is not None and not crosses_nested_repository(path, covering):
                overlap_count += 1
                continue
            scan_roots.append(path)
            try:
                snapshot = self._lstat(path)
            except OSError as error:
                diagnostics.append(stat_diagnostic(path, path, error, is_root=True))
                continue
            root = RootSnapshot(path, snapshot)
            roots.append(root)
            if stat.S_ISDIR(snapshot.st_mode):
                covering_directories.append(path)
        return roots, tuple(scan_roots), overlap_count

    def _collect_root(
        self,
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
    ) -> None:
        root = root_snapshot.path
        root_type = entry_type(root_snapshot.snapshot.st_mode)

        def consume_eligible(item: PendingEntry) -> None:
            extract_entry(
                item,
                kinds,
                adapter=self._timestamp_adapter,
                accounting=accounting,
                observations=observations,
                diagnostics=diagnostics,
                observation_consumer=observation_consumer,
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
        probe = self._ignore_service.probe(root, is_directory=root_type is EntryType.DIRECTORY)
        if probe.repository is not None and (probe.repository.is_bare or is_within_git_admin(root, probe.repository)):
            accounting.discover(root)
            origin = build_origin(root, root, root_type)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
            capabilities.append(ignore_capability(root, respect_gitignore, probe, error=None))
            return

        inventory: GitFilesystemInventory | None = None
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
                    ignore_service=self._ignore_service,
                    lstat_reader=self._lstat,
                    eligible_consumer=consume_eligible,
                )
                if visit.error is None:
                    capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.warning))
                    return
                if type(self._ignore_service) is GitIgnoreService:
                    # The production inventory is transactional: no callbacks
                    # ran before this failure. Report the unavailable scope
                    # instead of rebuilding an unbounded in-memory fallback.
                    accounting.discover(root)
                    accounting.record(root, RecordDisposition.RECORD_ERROR)
                    diagnostics.append(ignore_diagnostic(root, visit.error, warning=False))
                    capabilities.append(ignore_capability(root, respect_gitignore, probe, error=visit.error))
                    return
            else:
                candidate_inventory = self._ignore_service.inventory(probe.repository, root)
                if candidate_inventory.error is None:
                    inventory = candidate_inventory

        inventory_ignored_seen: set[str] = set()
        defer_ignore_evaluation = respect_gitignore and probe.repository is not None and inventory is None

        def process_visible_entry(item: PendingEntry) -> None:
            if item.entry_type is EntryType.DIRECTORY and not include_directories:
                # Directories needed only to reach requested leaves are
                # traversal structure, not discovered metadata records.
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

        pending = discover_entries(
            root_snapshot,
            scandir_reader=self._scandir,
            excluder=excluder,
            accounting=accounting,
            entries=entries,
            diagnostics=diagnostics,
            repository=probe.repository,
            inventory=inventory,
            inventory_ignored_seen=inventory_ignored_seen,
            pending_consumer=None if defer_ignore_evaluation else process_visible_entry,
            include_directories=include_directories,
        )
        ignored: frozenset[Path] = frozenset()
        ignore_error = None
        if inventory is not None:
            account_inventory_ignored(
                root,
                inventory,
                seen=inventory_ignored_seen,
                excluder=excluder,
                accounting=accounting,
            )
            account_inventory_warning(root, inventory, accounting=accounting, diagnostics=diagnostics)
            ignore_error = inventory.warning
        elif respect_gitignore and probe.repository is not None:
            matches = self._ignore_service.ignored(
                probe.repository,
                tuple(IgnoreCandidate(item.path, item.is_directory) for item in pending),
                lexical_root=root,
            )
            ignored = matches.ignored_paths
            ignore_error = matches.error
            if ignore_error is not None:
                # A repository was successfully identified, so losing Git at
                # evaluation time makes a requested ignore policy incomplete.
                diagnostics.append(ignore_diagnostic(root, ignore_error, warning=False))
        elif respect_gitignore and probe.error is not None:
            ignore_error = probe.error
            visible_repository = has_repository_marker_ancestor(
                root if root_type is EntryType.DIRECTORY else root.parent
            )
            diagnostics.append(
                ignore_diagnostic(
                    root,
                    probe.error,
                    warning=probe.error.unavailable and not visible_repository,
                )
            )
        capabilities.append(ignore_capability(root, respect_gitignore, probe, error=ignore_error))

        for item in pending:
            if item.path in ignored:
                if item.entry_type is EntryType.DIRECTORY and not include_directories:
                    continue
                disposition = RecordDisposition.IGNORED
            else:
                process_visible_entry(item)
                continue
            accounting.discover(root)
            accounting.record(root, disposition)
            retain_entry(entries, item.origin, disposition)



__all__ = [
    "CollectedFilesystemEntry",
    "DirectorySafetyError",
    "FilesystemAccounting",
    "FilesystemCollectionResult",
    "FilesystemCollector",
    "FilesystemObservationConsumer",
    "TimestampExtractionCoverage",
    "scandir_no_follow",
]
