"""Current-snapshot filesystem timestamp collection orchestration."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path

from workfold.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer
from workfold.collection.filesystem.accounting import AccountingBuilder
from workfold.collection.filesystem.entries import crosses_nested_repository, is_lexical_descendant, stat_diagnostic
from workfold.collection.filesystem.ignore import ExplicitExcluder, GitIgnoreService
from workfold.collection.filesystem.linux import LinuxStatxReader
from workfold.collection.filesystem.metadata import FilesystemTimestampAdapter
from workfold.collection.filesystem.models import (
    CollectedFilesystemEntry,
    FilesystemAccounting,
    FilesystemCollectionResult,
    TimestampExtractionCoverage,
)
from workfold.collection.filesystem.root import collect_root
from workfold.collection.filesystem.scan import (
    DirectorySafetyError,
    FilesystemObservationConsumer,
    LstatReader,
    RootSnapshot,
    ScandirReader,
    lstat,
    lstat_with_birthtime,
)
from workfold.collection.filesystem.traversal import scandir_no_follow
from workfold.domain.coverage import Capability
from workfold.domain.observations import EntryType, Source, TimestampKind, TimestampObservation
from workfold.domain.provenance import lexical_absolute
from workfold.domain.scope import ObservationScope


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
        entry_timestamps: Sequence[tuple[EntryType, Sequence[TimestampKind]]],
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
        selection = _normalize_entry_timestamps(entry_timestamps)
        kinds = tuple(
            kind for kind in TimestampKind if any(kind in selected_kinds for selected_kinds in selection.values())
        )
        excluder = ExplicitExcluder.compile(exclusions)
        base = lexical_absolute(cwd or Path.cwd())
        requested = tuple(lexical_absolute(path.expanduser(), base=base) for path in paths)
        native_birthtime_reader = self._timestamp_adapter.linux_birthtime_reader
        lstat_reader: LstatReader = self._lstat
        combined_statx_reader = (
            native_birthtime_reader
            if self._lstat is lstat
            and TimestampKind.FS_CREATED in kinds
            and self._timestamp_adapter.is_linux
            and self._timestamp_adapter.created_supported is not False
            and isinstance(native_birthtime_reader, LinuxStatxReader)
            else None
        )
        if combined_statx_reader is not None:
            lstat_reader = partial(lstat_with_birthtime, reader=combined_statx_reader)
        scandir_reader: ScandirReader = self._scandir
        if combined_statx_reader is not None and self._scandir is scandir_no_follow:
            scandir_reader = partial(scandir_no_follow, statx_reader=combined_statx_reader)

        diagnostics = DiagnosticBuffer()
        roots, scan_roots, overlap_count = self._prepare_roots(requested, diagnostics, lstat_reader)
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
            accounting.ensure_root(root, selection)
            capabilities.extend(
                self._timestamp_adapter.capability(
                    kind,
                    target=os.fspath(root),
                    snapshot=root_snapshot.snapshot,
                )
                for kind in kinds
            )
            collect_root(
                root_snapshot,
                entry_timestamps=selection,
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
                lstat_reader=lstat_reader,
                scandir_reader=scandir_reader,
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
        lstat_reader: LstatReader,
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
                snapshot = lstat_reader(path)
            except OSError as error:
                diagnostics.append(stat_diagnostic(path, path, error, is_root=True))
                continue
            roots.append(RootSnapshot(path, snapshot))
            if stat.S_ISDIR(snapshot.st_mode):
                covering_directories.append(path)
        return roots, tuple(scan_roots), overlap_count


def _normalize_entry_timestamps(
    values: Sequence[tuple[EntryType, Sequence[TimestampKind]]],
) -> Mapping[EntryType, tuple[TimestampKind, ...]]:
    """Validate and canonicalize an exact entry-type/timestamp matrix."""

    selected: dict[EntryType, set[TimestampKind]] = {}
    for raw_entry_type, timestamp_kinds in values:
        entry_type = _require_entry_type(raw_entry_type)
        kinds = tuple(_require_timestamp_kind(kind) for kind in timestamp_kinds)
        if not kinds:
            raise ValueError(f"filesystem entry type {entry_type.value!r} needs at least one timestamp kind")
        if any(kind.source is not Source.FILESYSTEM for kind in kinds):
            raise ValueError("filesystem collector accepts only filesystem timestamp kinds")
        selected.setdefault(entry_type, set()).update(kinds)
    if not selected:
        raise ValueError("filesystem collection needs at least one entry/timestamp selection")
    return {
        entry_type: tuple(kind for kind in TimestampKind if kind in selected[entry_type])
        for entry_type in EntryType
        if entry_type in selected
    }


def _require_entry_type(value: object) -> EntryType:
    if not isinstance(value, EntryType):
        raise TypeError("filesystem entry types must be EntryType values")
    return value


def _require_timestamp_kind(value: object) -> TimestampKind:
    if not isinstance(value, TimestampKind):
        raise TypeError("filesystem timestamp kinds must be TimestampKind values")
    return value


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
