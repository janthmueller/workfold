"""Current-snapshot filesystem timestamp collection orchestration."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.filesystem.accounting import AccountingBuilder
from workfold.collectors.filesystem.helpers import crosses_nested_repository, is_lexical_descendant, stat_diagnostic
from workfold.collectors.filesystem.models import (
    CollectedFilesystemEntry,
    FilesystemAccounting,
    FilesystemCollectionResult,
    TimestampExtractionCoverage,
)
from workfold.collectors.filesystem.root_collector import collect_root
from workfold.collectors.filesystem.traversal import scandir_no_follow
from workfold.collectors.filesystem.types import (
    DirectorySafetyError,
    FilesystemObservationConsumer,
    LstatReader,
    RootSnapshot,
    ScandirReader,
    lstat,
)
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.collectors.ignores import ExplicitExcluder, GitIgnoreService
from workfold.coverage import Capability
from workfold.models import Source, TimestampKind, TimestampObservation
from workfold.provenance import lexical_absolute
from workfold.scope import ObservationScope


class FilesystemCollector:
    """Schedule exact roots and collect their current metadata observations."""

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
        observation_scope: ObservationScope | None = None,
        retain_entries: bool = True,
        retain_observations: bool = True,
    ) -> FilesystemCollectionResult:
        """Extract requested metadata slots and emit those matching the scope."""

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
        requested = tuple(lexical_absolute(path.expanduser(), base=base) for path in paths)

        diagnostics = DiagnosticBuffer()
        roots, scan_roots, overlap_count = self._prepare_roots(requested, diagnostics)
        entries: list[CollectedFilesystemEntry] | None = [] if retain_entries else None
        observations: list[TimestampObservation] | None = [] if retain_observations else None
        capabilities: list[Capability] = []
        accounting = AccountingBuilder(retain_scope_match_ids=retain_observations)

        queued_roots = [(item, excluder) for item in roots]
        scheduled_roots = {os.path.normcase(os.fspath(item.path)) for item in roots}
        actual_scan_roots = list(scan_roots)
        successful_roots: list[Path] = []

        def queue_nested_repository(root_snapshot: RootSnapshot, nested_excluder: ExplicitExcluder) -> None:
            key = os.path.normcase(os.fspath(root_snapshot.path))
            if key in scheduled_roots:
                return
            scheduled_roots.add(key)
            queued_roots.append((root_snapshot, nested_excluder))
            actual_scan_roots.append(root_snapshot.path)

        for root_snapshot, root_excluder in queued_roots:
            root = root_snapshot.path
            successful_roots.append(root)
            accounting.ensure_root(root, kinds)
            capabilities.extend(self._timestamp_adapter.capability(kind, target=os.fspath(root)) for kind in kinds)
            collect_root(
                root_snapshot,
                kinds=kinds,
                include_regular_files=include_regular_files,
                include_directories=include_directories,
                include_symlinks=include_symlinks,
                respect_gitignore=respect_gitignore,
                excluder=root_excluder,
                accounting=accounting,
                entries=entries,
                observations=observations,
                capabilities=capabilities,
                diagnostics=diagnostics,
                observation_consumer=observation_consumer,
                observation_scope=observation_scope,
                nested_repository_consumer=queue_nested_repository,
                timestamp_adapter=self._timestamp_adapter,
                ignore_service=self._ignore_service,
                lstat_reader=self._lstat,
                scandir_reader=self._scandir,
            )

        return FilesystemCollectionResult(
            entries=tuple(entries or ()),
            observations=tuple(observations or ()),
            accounting=accounting.build(),
            capabilities=tuple(capabilities),
            diagnostics=diagnostics.snapshot(),
            requested_roots=requested,
            scan_roots=tuple(actual_scan_roots),
            successful_roots=tuple(successful_roots),
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
            covering = next(
                (root for root in reversed(covering_directories) if is_lexical_descendant(path, root)),
                None,
            )
            if covering is not None and not crosses_nested_repository(path, covering):
                overlap_count += 1
                continue
            scan_roots.append(path)
            try:
                snapshot = self._lstat(path)
            except OSError as error:
                diagnostics.append(stat_diagnostic(path, path, error, is_root=True))
                continue
            roots.append(RootSnapshot(path, snapshot))
            if stat.S_ISDIR(snapshot.st_mode):
                covering_directories.append(path)
        return roots, tuple(scan_roots), overlap_count


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
