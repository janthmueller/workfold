"""Current-snapshot filesystem timestamp collection.

Git-aware scans use Git only to inventory paths under its authoritative ignore
semantics; a current no-follow stat remains the entry/type/timestamp boundary.
Native traversal is lexical and never follows symbolic-link targets. Every
discovered entry receives one record disposition and every eligible timestamp
slot receives one extraction disposition. Portable timestamps share the
discovery stat snapshot; Linux birth time adds one identity-checked statx read.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.collectors.ignores import (
    ExplicitExcluder,
    GitFilesystemInventory,
    GitFilesystemInventoryVisit,
    GitIgnoreProbe,
    GitIgnoreRepository,
    GitIgnoreService,
    IgnoreCandidate,
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_git_admin_name,
    is_git_admin_path,
    is_nested_repository_boundary,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.coverage import (
    Capability,
    CapabilityStatus,
    CoverageLedger,
    CoverageLedgerBuilder,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    SelectionDisposition,
    TimestampCoverageKey,
)
from workfold.models import EntryType, RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import absolute_filesystem_entry_id, lexical_absolute


@dataclass(frozen=True, slots=True)
class TimestampExtractionCoverage:
    """Extraction-only accounting retained until common selection completes."""

    key: TimestampCoverageKey
    requested: int
    captured: int
    unavailable: int
    unsupported: int
    errors: int
    observation_ids: tuple[str, ...]
    observation_ids_complete: bool = True

    def __post_init__(self) -> None:
        values = (self.requested, self.captured, self.unavailable, self.unsupported, self.errors)
        if any(value < 0 for value in values):
            raise ValueError("filesystem extraction counts must be non-negative")
        if self.requested != self.captured + self.unavailable + self.unsupported + self.errors:
            raise ValueError("filesystem extraction accounting does not reconcile")
        if self.observation_ids_complete and self.captured != len(self.observation_ids):
            raise ValueError("captured count must match the retained observation identities")
        if len(self.observation_ids) > self.captured:
            raise ValueError("retained observation identities cannot exceed captured timestamps")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("filesystem extraction accounting contains duplicate observations")

    def count(self, disposition: ExtractionDisposition) -> int:
        """Return the count for one extraction disposition."""

        return {
            ExtractionDisposition.CAPTURED: self.captured,
            ExtractionDisposition.UNAVAILABLE: self.unavailable,
            ExtractionDisposition.UNSUPPORTED: self.unsupported,
            ExtractionDisposition.ERROR: self.errors,
        }[disposition]


@dataclass(frozen=True, slots=True)
class FilesystemAccounting:
    """Reconciled discovery/extraction counts before selection and plotting."""

    records: tuple[RecordCoverage, ...]
    timestamps: tuple[TimestampExtractionCoverage, ...]

    def __post_init__(self) -> None:
        if len({item.key for item in self.records}) != len(self.records):
            raise ValueError("filesystem record accounting contains duplicate keys")
        if len({item.key for item in self.timestamps}) != len(self.timestamps):
            raise ValueError("filesystem timestamp accounting contains duplicate keys")
        records_by_key = {item.key: item for item in self.records}
        for item in self.records:
            item.validate()
        for item in self.timestamps:
            record_key = RecordCoverageKey(item.key.source, item.key.target, item.key.record_kind)
            record = records_by_key.get(record_key)
            if record is None:
                raise ValueError("filesystem timestamp accounting has no record partition")
            if item.requested != record.eligible:
                raise ValueError("filesystem timestamp slots must equal eligible filesystem records")

    def build_coverage(
        self,
        selection_by_observation_id: Mapping[str, SelectionDisposition],
        plotting_by_observation_id: Mapping[str, PlottingDisposition],
    ) -> CoverageLedger:
        """Complete the ledger after common date selection and marker creation.

        Exact key equality is required, so an orchestration bug cannot silently
        omit or invent an observation at the collector/application boundary.
        """

        if any(not item.observation_ids_complete for item in self.timestamps):
            raise ValueError("observation-ID coverage is unavailable for a non-retaining collection")
        expected = {observation_id for item in self.timestamps for observation_id in item.observation_ids}
        supplied = set(selection_by_observation_id)
        if supplied != expected:
            missing = len(expected - supplied)
            extra = len(supplied - expected)
            raise ValueError(
                f"selection map must cover every captured filesystem observation (missing={missing}, extra={extra})"
            )
        included = {
            observation_id
            for observation_id, disposition in selection_by_observation_id.items()
            if disposition is SelectionDisposition.INCLUDED
        }
        plotted = set(plotting_by_observation_id)
        if plotted != included:
            missing = len(included - plotted)
            extra = len(plotted - included)
            raise ValueError(
                f"plotting map must cover every included filesystem observation (missing={missing}, extra={extra})"
            )

        builder = CoverageLedgerBuilder()
        for item in self.records:
            builder.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                builder.record_outcome(item.key, disposition, item.count(disposition))
        for item in self.timestamps:
            builder.request_slot(item.key, item.requested)
            for disposition in ExtractionDisposition:
                builder.extraction_outcome(item.key, disposition, item.count(disposition))
            for observation_id in item.observation_ids:
                selection = selection_by_observation_id[observation_id]
                builder.selection_outcome(item.key, selection)
                if selection is SelectionDisposition.INCLUDED:
                    builder.plotting_outcome(item.key, plotting_by_observation_id[observation_id])
        return builder.build()

    def build_coverage_counts(
        self,
        selection_counts: Mapping[tuple[TimestampCoverageKey, SelectionDisposition], int],
        plotting_counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    ) -> CoverageLedger:
        """Complete coverage from partition counts without retaining observation IDs."""

        builder = CoverageLedgerBuilder()
        for item in self.records:
            builder.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                builder.record_outcome(item.key, disposition, item.count(disposition))
        for item in self.timestamps:
            builder.request_slot(item.key, item.requested)
            for disposition in ExtractionDisposition:
                builder.extraction_outcome(item.key, disposition, item.count(disposition))
            for disposition in SelectionDisposition:
                builder.selection_outcome(
                    item.key,
                    disposition,
                    selection_counts.get((item.key, disposition), 0),
                )
            for disposition in PlottingDisposition:
                builder.plotting_outcome(
                    item.key,
                    disposition,
                    plotting_counts.get((item.key, disposition), 0),
                )
        return builder.build()


@dataclass(frozen=True, slots=True)
class CollectedFilesystemEntry:
    """A stat-successful entry and its terminal discovery disposition."""

    origin: RecordOrigin
    disposition: RecordDisposition


@dataclass(frozen=True, slots=True)
class FilesystemCollectionResult:
    """Domain observations plus honest filesystem-specific accounting."""

    entries: tuple[CollectedFilesystemEntry, ...]
    observations: tuple[TimestampObservation, ...]
    accounting: FilesystemAccounting
    capabilities: tuple[Capability, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_roots: tuple[Path, ...]
    scan_roots: tuple[Path, ...]
    successful_roots: tuple[Path, ...]
    overlapping_roots_deduplicated: int = 0

    @property
    def origins(self) -> tuple[RecordOrigin, ...]:
        """Return provenance for every stat-successful discovered record."""

        return tuple(item.origin for item in self.entries)

    @property
    def eligible_origins(self) -> tuple[RecordOrigin, ...]:
        """Return only origins for which timestamp slots were requested."""

        return tuple(item.origin for item in self.entries if item.disposition is RecordDisposition.ELIGIBLE)

    @property
    def is_partial(self) -> bool:
        """Return whether an operational error prevented complete collection."""

        return any(item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics)

    def build_coverage(
        self,
        selection_by_observation_id: Mapping[str, SelectionDisposition],
        plotting_by_observation_id: Mapping[str, PlottingDisposition],
    ) -> CoverageLedger:
        """Delegate completion of the common coverage ledger."""

        return self.accounting.build_coverage(selection_by_observation_id, plotting_by_observation_id)

    def build_coverage_counts(
        self,
        selection_counts: Mapping[tuple[TimestampCoverageKey, SelectionDisposition], int],
        plotting_counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    ) -> CoverageLedger:
        """Complete the ledger from bounded-memory pipeline counters."""

        return self.accounting.build_coverage_counts(selection_counts, plotting_counts)


@dataclass(frozen=True, slots=True)
class _RootSnapshot:
    path: Path
    snapshot: os.stat_result


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    root: Path
    path: Path
    snapshot: os.stat_result
    origin: RecordOrigin
    entry_type: EntryType | None

    @property
    def is_directory(self) -> bool:
        return self.entry_type is EntryType.DIRECTORY


@dataclass(slots=True)
class _AccountingBuilder:
    retain_observation_ids: bool = True
    _discovered: dict[RecordCoverageKey, int] = field(default_factory=lambda: {})
    _records: dict[tuple[RecordCoverageKey, RecordDisposition], int] = field(default_factory=lambda: {})
    _requested: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _extractions: dict[tuple[TimestampCoverageKey, ExtractionDisposition], int] = field(default_factory=lambda: {})
    _observation_ids: dict[TimestampCoverageKey, list[str]] = field(default_factory=lambda: {})
    _record_keys: dict[Path, RecordCoverageKey] = field(default_factory=lambda: {})
    _timestamp_keys: dict[tuple[Path, TimestampKind], TimestampCoverageKey] = field(default_factory=lambda: {})

    def _record_key(self, root: Path) -> RecordCoverageKey:
        key = self._record_keys.get(root)
        if key is None:
            key = _record_key(root)
            self._record_keys[root] = key
        return key

    def _timestamp_key(self, root: Path, kind: TimestampKind) -> TimestampCoverageKey:
        partition = (root, kind)
        key = self._timestamp_keys.get(partition)
        if key is None:
            key = _timestamp_key(root, kind)
            self._timestamp_keys[partition] = key
        return key

    def ensure_root(self, root: Path, kinds: Sequence[TimestampKind]) -> None:
        record_key = self._record_key(root)
        self._discovered.setdefault(record_key, 0)
        for kind in kinds:
            key = self._timestamp_key(root, kind)
            self._requested.setdefault(key, 0)
            self._observation_ids.setdefault(key, [])

    def discover(self, root: Path, count: int = 1) -> None:
        if count < 0:
            raise ValueError("filesystem discovery count must be non-negative")
        key = self._record_key(root)
        self._discovered[key] = self._discovered.get(key, 0) + count

    def record(self, root: Path, disposition: RecordDisposition, count: int = 1) -> None:
        if count < 0:
            raise ValueError("filesystem record count must be non-negative")
        key = (self._record_key(root), disposition)
        self._records[key] = self._records.get(key, 0) + count

    def request(self, root: Path, kind: TimestampKind) -> None:
        key = self._timestamp_key(root, kind)
        self._requested[key] = self._requested.get(key, 0) + 1

    def extraction(
        self,
        root: Path,
        kind: TimestampKind,
        disposition: ExtractionDisposition,
        observation_id: str | None = None,
    ) -> None:
        key = self._timestamp_key(root, kind)
        outcome = (key, disposition)
        self._extractions[outcome] = self._extractions.get(outcome, 0) + 1
        if observation_id is not None and self.retain_observation_ids:
            if disposition is not ExtractionDisposition.CAPTURED:
                raise ValueError("only captured outcomes may retain observation identities")
            self._observation_ids.setdefault(key, []).append(observation_id)

    def build(self) -> FilesystemAccounting:
        record_keys = set(self._discovered)
        record_keys.update(key for key, _ in self._records)
        timestamp_keys = set(self._requested)
        timestamp_keys.update(key for key, _ in self._extractions)
        records = tuple(
            RecordCoverage(
                key=key,
                discovered=self._discovered.get(key, 0),
                eligible=self._records.get((key, RecordDisposition.ELIGIBLE), 0),
                ignored=self._records.get((key, RecordDisposition.IGNORED), 0),
                explicitly_excluded=self._records.get((key, RecordDisposition.EXPLICITLY_EXCLUDED), 0),
                excluded_entry_type=self._records.get((key, RecordDisposition.EXCLUDED_ENTRY_TYPE), 0),
                semantic_git_admin=self._records.get((key, RecordDisposition.SEMANTIC_GIT_ADMIN), 0),
                record_errors=self._records.get((key, RecordDisposition.RECORD_ERROR), 0),
            )
            for key in sorted(record_keys, key=lambda item: item.target)
        )
        timestamps = tuple(
            TimestampExtractionCoverage(
                key=key,
                requested=self._requested.get(key, 0),
                captured=self._extractions.get((key, ExtractionDisposition.CAPTURED), 0),
                unavailable=self._extractions.get((key, ExtractionDisposition.UNAVAILABLE), 0),
                unsupported=self._extractions.get((key, ExtractionDisposition.UNSUPPORTED), 0),
                errors=self._extractions.get((key, ExtractionDisposition.ERROR), 0),
                observation_ids=tuple(self._observation_ids.get(key, ())),
                observation_ids_complete=self.retain_observation_ids,
            )
            for key in sorted(timestamp_keys, key=lambda item: (item.target, item.timestamp_kind.value))
        )
        return FilesystemAccounting(records, timestamps)


LstatReader = Callable[[Path], os.stat_result]
ScandirContext = AbstractContextManager[Iterator[os.DirEntry[str]]]
ScandirReader = Callable[[Path], ScandirContext]
FilesystemObservationConsumer = Callable[[tuple[TimestampObservation, ...]], None]


def _lstat(path: Path) -> os.stat_result:
    return os.lstat(path)


class DirectorySafetyError(OSError):
    """A queued directory no longer satisfies no-follow traversal safety."""


@contextmanager
def scandir_no_follow(path: Path) -> Generator[Iterator[os.DirEntry[str]], None, None]:
    """Open a directory without following a replacement final symlink.

    POSIX-capable interpreters use a no-follow directory descriptor. The
    fallback validates the final entry both before and after opening and never
    yields an iterator when the entry is no longer a directory.
    """

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


class FilesystemCollector:
    """Collect current metadata observations beneath exact selected roots."""

    def __init__(
        self,
        *,
        timestamp_adapter: FilesystemTimestampAdapter | None = None,
        ignore_service: GitIgnoreService | None = None,
        lstat_reader: LstatReader = _lstat,
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
        accounting = _AccountingBuilder(retain_observation_ids=retain_observations)

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
    ) -> tuple[list[_RootSnapshot], tuple[Path, ...], int]:
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

        roots: list[_RootSnapshot] = []
        scan_roots: list[Path] = []
        covering_directories: list[Path] = []
        for _, path in indexed:
            covering = next((root for root in covering_directories if _is_lexical_descendant(path, root)), None)
            if covering is not None and not _crosses_nested_repository(path, covering):
                overlap_count += 1
                continue
            scan_roots.append(path)
            try:
                snapshot = self._lstat(path)
            except OSError as error:
                diagnostics.append(_stat_diagnostic(path, path, error, is_root=True))
                continue
            root = _RootSnapshot(path, snapshot)
            roots.append(root)
            if stat.S_ISDIR(snapshot.st_mode):
                covering_directories.append(path)
        return roots, tuple(scan_roots), overlap_count

    def _collect_root(
        self,
        root_snapshot: _RootSnapshot,
        *,
        kinds: tuple[TimestampKind, ...],
        include_regular_files: bool,
        include_directories: bool,
        include_symlinks: bool,
        respect_gitignore: bool,
        excluder: ExplicitExcluder,
        accounting: _AccountingBuilder,
        entries: list[CollectedFilesystemEntry] | None,
        observations: list[TimestampObservation] | None,
        capabilities: list[Capability],
        diagnostics: list[CollectorDiagnostic],
        observation_consumer: FilesystemObservationConsumer | None,
    ) -> None:
        root = root_snapshot.path
        root_type = _entry_type(root_snapshot.snapshot.st_mode)
        if has_git_admin_ancestor(root) or (root_type is EntryType.DIRECTORY and looks_like_bare_repository(root)):
            accounting.discover(root)
            origin = _origin(root, root, root_type)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            _retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
            capabilities.append(
                _ignore_capability(
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
            origin = _origin(root, root, root_type)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            _retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
            capabilities.append(_ignore_capability(root, respect_gitignore, probe, error=None))
            return

        inventory: GitFilesystemInventory | None = None
        if respect_gitignore and probe.repository is not None and root_type is EntryType.DIRECTORY:
            if not include_directories:
                visit = self._collect_git_inventory_stream(
                    root_snapshot,
                    repository=probe.repository,
                    kinds=kinds,
                    include_regular_files=include_regular_files,
                    include_symlinks=include_symlinks,
                    excluder=excluder,
                    accounting=accounting,
                    entries=entries,
                    observations=observations,
                    diagnostics=diagnostics,
                    observation_consumer=observation_consumer,
                )
                if visit.error is None:
                    capabilities.append(_ignore_capability(root, respect_gitignore, probe, error=visit.warning))
                    return
                if type(self._ignore_service) is GitIgnoreService:
                    # The production inventory is transactional: no callbacks
                    # ran before this failure. Report the unavailable scope
                    # instead of rebuilding an unbounded in-memory fallback.
                    accounting.discover(root)
                    accounting.record(root, RecordDisposition.RECORD_ERROR)
                    diagnostics.append(_ignore_diagnostic(root, visit.error, warning=False))
                    capabilities.append(_ignore_capability(root, respect_gitignore, probe, error=visit.error))
                    return
            else:
                candidate_inventory = self._ignore_service.inventory(probe.repository, root)
                if candidate_inventory.error is None:
                    inventory = candidate_inventory

        inventory_ignored_seen: set[str] = set()
        defer_ignore_evaluation = respect_gitignore and probe.repository is not None and inventory is None

        def process_visible_entry(item: _PendingEntry) -> None:
            if item.entry_type is EntryType.DIRECTORY and not include_directories:
                # Directories needed only to reach requested leaves are
                # traversal structure, not discovered metadata records.
                return
            disposition = (
                RecordDisposition.ELIGIBLE
                if _entry_is_in_scope(
                    item.entry_type,
                    include_regular_files=include_regular_files,
                    include_directories=include_directories,
                    include_symlinks=include_symlinks,
                )
                else RecordDisposition.EXCLUDED_ENTRY_TYPE
            )
            accounting.discover(root)
            accounting.record(root, disposition)
            _retain_entry(entries, item.origin, disposition)
            if disposition is RecordDisposition.ELIGIBLE:
                self._extract_entry(
                    item,
                    kinds,
                    accounting,
                    observations,
                    diagnostics,
                    observation_consumer,
                )

        pending = self._discover_entries(
            root_snapshot,
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
            _account_inventory_ignored(
                root,
                inventory,
                seen=inventory_ignored_seen,
                excluder=excluder,
                accounting=accounting,
            )
            _account_inventory_warning(root, inventory, accounting=accounting, diagnostics=diagnostics)
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
                diagnostics.append(_ignore_diagnostic(root, ignore_error, warning=False))
        elif respect_gitignore and probe.error is not None:
            ignore_error = probe.error
            visible_repository = has_repository_marker_ancestor(
                root if root_type is EntryType.DIRECTORY else root.parent
            )
            diagnostics.append(
                _ignore_diagnostic(
                    root,
                    probe.error,
                    warning=probe.error.unavailable and not visible_repository,
                )
            )
        capabilities.append(_ignore_capability(root, respect_gitignore, probe, error=ignore_error))

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
            _retain_entry(entries, item.origin, disposition)

    def _collect_git_inventory(
        self,
        root_snapshot: _RootSnapshot,
        *,
        inventory: GitFilesystemInventory,
        repository: GitIgnoreRepository,
        kinds: tuple[TimestampKind, ...],
        include_regular_files: bool,
        include_symlinks: bool,
        excluder: ExplicitExcluder,
        accounting: _AccountingBuilder,
        entries: list[CollectedFilesystemEntry] | None,
        observations: list[TimestampObservation] | None,
        diagnostics: list[CollectorDiagnostic],
        observation_consumer: FilesystemObservationConsumer | None,
    ) -> None:
        """Validate Git path candidates against the current filesystem snapshot."""

        root = root_snapshot.path
        _account_inventory_warning(root, inventory, accounting=accounting, diagnostics=diagnostics)

        try:
            selected_is_worktree_root = root.resolve(strict=True) == repository.root.resolve(strict=True)
        except (OSError, RuntimeError):
            selected_is_worktree_root = False
        if selected_is_worktree_root:
            admin_path = root / ".git"
            try:
                admin_snapshot = self._lstat(admin_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(_stat_diagnostic(root, admin_path, error, is_root=False))
            else:
                accounting.discover(root)
                admin_origin = _origin(root, admin_path, _entry_type(admin_snapshot.st_mode))
                accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                _retain_entry(entries, admin_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)

        for relative_path in inventory.included_relative_paths:
            path = root if relative_path == "." else root / relative_path
            try:
                snapshot = self._lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                # The Git index can retain a path that is absent from the
                # current worktree. It is a candidate, not a filesystem record.
                continue
            except OSError as error:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(_stat_diagnostic(root, path, error, is_root=False))
                continue

            accounting.discover(root)
            entry_type = _entry_type(snapshot.st_mode)
            origin = _origin(root, path, entry_type)
            relative = PurePosixPath(relative_path)
            if _is_semantic_git_admin(path, repository) or (
                entry_type is EntryType.DIRECTORY and is_nested_repository_boundary(path, selected_root=root)
            ):
                disposition = RecordDisposition.SEMANTIC_GIT_ADMIN
            elif excluder.matches(relative, is_directory=entry_type is EntryType.DIRECTORY):
                disposition = RecordDisposition.EXPLICITLY_EXCLUDED
            elif _entry_is_in_scope(
                entry_type,
                include_regular_files=include_regular_files,
                include_directories=False,
                include_symlinks=include_symlinks,
            ):
                disposition = RecordDisposition.ELIGIBLE
            else:
                disposition = RecordDisposition.EXCLUDED_ENTRY_TYPE
            accounting.record(root, disposition)
            _retain_entry(entries, origin, disposition)
            if disposition is RecordDisposition.ELIGIBLE:
                self._extract_entry(
                    _PendingEntry(root, path, snapshot, origin, entry_type),
                    kinds,
                    accounting,
                    observations,
                    diagnostics,
                    observation_consumer,
                )

        _account_inventory_ignored(
            root,
            inventory,
            seen=set(),
            excluder=excluder,
            accounting=accounting,
        )

    def _collect_git_inventory_stream(
        self,
        root_snapshot: _RootSnapshot,
        *,
        repository: GitIgnoreRepository,
        kinds: tuple[TimestampKind, ...],
        include_regular_files: bool,
        include_symlinks: bool,
        excluder: ExplicitExcluder,
        accounting: _AccountingBuilder,
        entries: list[CollectedFilesystemEntry] | None,
        observations: list[TimestampObservation] | None,
        diagnostics: list[CollectorDiagnostic],
        observation_consumer: FilesystemObservationConsumer | None,
    ) -> GitFilesystemInventoryVisit:
        """Consume the default Git inventory through a bounded disk spool."""

        root = root_snapshot.path

        def consume_included(relative_path: str) -> None:
            path = root if relative_path == "." else root / relative_path
            try:
                snapshot = self._lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                # Index entries absent from the current worktree are candidates,
                # not current filesystem records.
                return
            except OSError as error:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(_stat_diagnostic(root, path, error, is_root=False))
                return

            accounting.discover(root)
            entry_type = _entry_type(snapshot.st_mode)
            origin = _origin(root, path, entry_type)
            relative = PurePosixPath(relative_path)
            if _is_semantic_git_admin(path, repository) or (
                entry_type is EntryType.DIRECTORY and is_nested_repository_boundary(path, selected_root=root)
            ):
                disposition = RecordDisposition.SEMANTIC_GIT_ADMIN
            elif excluder.matches(relative, is_directory=entry_type is EntryType.DIRECTORY):
                disposition = RecordDisposition.EXPLICITLY_EXCLUDED
            elif _entry_is_in_scope(
                entry_type,
                include_regular_files=include_regular_files,
                include_directories=False,
                include_symlinks=include_symlinks,
            ):
                disposition = RecordDisposition.ELIGIBLE
            else:
                disposition = RecordDisposition.EXCLUDED_ENTRY_TYPE
            accounting.record(root, disposition)
            _retain_entry(entries, origin, disposition)
            if disposition is RecordDisposition.ELIGIBLE:
                self._extract_entry(
                    _PendingEntry(root, path, snapshot, origin, entry_type),
                    kinds,
                    accounting,
                    observations,
                    diagnostics,
                    observation_consumer,
                )

        def consume_ignored(relative_path: str, is_directory: bool) -> None:
            disposition = (
                RecordDisposition.EXPLICITLY_EXCLUDED
                if excluder.matches(relative_path, is_directory=is_directory)
                else RecordDisposition.IGNORED
            )
            accounting.discover(root)
            accounting.record(root, disposition)

        visit = self._ignore_service.visit_inventory(
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
                admin_snapshot = self._lstat(admin_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                accounting.discover(root)
                accounting.record(root, RecordDisposition.RECORD_ERROR)
                diagnostics.append(_stat_diagnostic(root, admin_path, error, is_root=False))
            else:
                accounting.discover(root)
                admin_origin = _origin(root, admin_path, _entry_type(admin_snapshot.st_mode))
                accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                _retain_entry(entries, admin_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)

        if visit.warning is not None:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.RECORD_ERROR)
            diagnostics.append(_ignore_diagnostic(root, visit.warning, warning=False))
        return visit

    def _discover_entries(
        self,
        root_snapshot: _RootSnapshot,
        *,
        excluder: ExplicitExcluder,
        accounting: _AccountingBuilder,
        entries: list[CollectedFilesystemEntry] | None,
        diagnostics: list[CollectorDiagnostic],
        repository: GitIgnoreRepository | None,
        inventory: GitFilesystemInventory | None,
        inventory_ignored_seen: set[str],
        pending_consumer: Callable[[_PendingEntry], None] | None,
        include_directories: bool,
    ) -> list[_PendingEntry]:
        root = root_snapshot.path
        pending: list[_PendingEntry] = []
        root_type = _entry_type(root_snapshot.snapshot.st_mode)
        root_origin = _origin(root, root, root_type)
        root_explicit = root_type is not EntryType.DIRECTORY and excluder.matches(
            PurePosixPath(root.name),
            is_directory=False,
        )
        if _is_semantic_git_admin(root, repository):
            accounting.discover(root)
            accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
            _retain_entry(entries, root_origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
            return pending
        if root_explicit:
            accounting.discover(root)
            accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
            _retain_entry(entries, root_origin, RecordDisposition.EXPLICITLY_EXCLUDED)
            return pending
        _queue_or_consume(
            _PendingEntry(root, root, root_snapshot.snapshot, root_origin, root_type),
            pending,
            pending_consumer,
        )
        if root_type is not EntryType.DIRECTORY:
            return pending

        inventory_ignored_paths: set[str] = set(inventory.ignored_relative_paths) if inventory is not None else set()
        inventory_directory_paths: frozenset[str] = (
            inventory.ignored_directory_paths if inventory is not None else frozenset()
        )
        admin_relative_parts = _repository_admin_relative_parts(root, repository)
        root_relative = PurePosixPath(".")
        directories = [(root, root_relative)]
        while directories:
            directory, directory_relative = directories.pop()
            try:
                with self._scandir(directory) as iterator:
                    try:
                        for directory_entry in iterator:
                            name = directory_entry.name
                            path = directory / name
                            try:
                                snapshot = directory_entry.stat(follow_symlinks=False)
                            except OSError as error:
                                accounting.discover(root)
                                accounting.record(root, RecordDisposition.RECORD_ERROR)
                                diagnostics.append(_stat_diagnostic(root, path, error, is_root=False))
                                continue

                            entry_type = _entry_type(snapshot.st_mode)
                            origin = _origin(root, path, entry_type)
                            relative = (
                                PurePosixPath(name)
                                if directory_relative == root_relative
                                else directory_relative / name
                            )
                            relative_text = relative.as_posix()
                            inventory_ignored = relative_text in inventory_ignored_paths
                            if inventory_ignored:
                                inventory_ignored_seen.add(relative_text)
                            if _is_semantic_git_admin(
                                path,
                                repository,
                                relative_parts=relative.parts,
                                admin_relative_parts=admin_relative_parts,
                            ):
                                accounting.discover(root)
                                accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                                _retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
                                continue
                            if excluder.matches(relative, is_directory=entry_type is EntryType.DIRECTORY):
                                accounting.discover(root)
                                accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED)
                                _retain_entry(entries, origin, RecordDisposition.EXPLICITLY_EXCLUDED)
                                # Explicitly excluded directories define scope
                                # boundaries: record the directory once and
                                # prune its subtree.
                                continue
                            if entry_type is EntryType.DIRECTORY and is_nested_repository_boundary(
                                path,
                                selected_root=root,
                            ):
                                accounting.discover(root)
                                accounting.record(root, RecordDisposition.SEMANTIC_GIT_ADMIN)
                                _retain_entry(entries, origin, RecordDisposition.SEMANTIC_GIT_ADMIN)
                                continue
                            if inventory_ignored or (
                                entry_type is EntryType.DIRECTORY and relative_text in inventory_directory_paths
                            ):
                                if entry_type is not EntryType.DIRECTORY or include_directories:
                                    accounting.discover(root)
                                    accounting.record(root, RecordDisposition.IGNORED)
                                    _retain_entry(entries, origin, RecordDisposition.IGNORED)
                                continue
                            _queue_or_consume(
                                _PendingEntry(root, path, snapshot, origin, entry_type),
                                pending,
                                pending_consumer,
                            )
                            if entry_type is EntryType.DIRECTORY:
                                directories.append((path, relative))
                    except OSError as error:
                        diagnostics.append(_traversal_diagnostic(root, directory, error))
            except OSError as error:
                diagnostics.append(_traversal_diagnostic(root, directory, error))
        return pending

    def _extract_entry(
        self,
        item: _PendingEntry,
        kinds: tuple[TimestampKind, ...],
        accounting: _AccountingBuilder,
        observations: list[TimestampObservation] | None,
        diagnostics: list[CollectorDiagnostic],
        observation_consumer: FilesystemObservationConsumer | None,
    ) -> None:
        captured: list[TimestampObservation] = []
        for kind in kinds:
            accounting.request(item.root, kind)
            extraction = self._timestamp_adapter.extract(item.snapshot, kind, path=item.path)
            if extraction.disposition is ExtractionDisposition.CAPTURED:
                if extraction.instant_utc_ns is None or extraction.raw_timestamp is None:
                    raise RuntimeError("captured timestamp extraction omitted its value")
                observation = TimestampObservation.create(
                    item.origin,
                    kind,
                    extraction.instant_utc_ns,
                    extraction.raw_timestamp,
                )
                captured.append(observation)
                if observations is not None:
                    observations.append(observation)
                accounting.extraction(
                    item.root,
                    kind,
                    extraction.disposition,
                    observation.observation_id,
                )
            else:
                accounting.extraction(item.root, kind, extraction.disposition)
                if extraction.disposition is ExtractionDisposition.ERROR:
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="filesystem_timestamp_error",
                            stage="filesystem_timestamp_extraction",
                            target=os.fspath(item.root),
                            message=extraction.note or "filesystem timestamp extraction failed",
                            path=os.fspath(item.path),
                            provenance_id=item.origin.record_id,
                        )
                    )
        if captured and observation_consumer is not None:
            observation_consumer(tuple(captured))


def _retain_entry(
    entries: list[CollectedFilesystemEntry] | None,
    origin: RecordOrigin,
    disposition: RecordDisposition,
) -> None:
    if entries is not None:
        entries.append(CollectedFilesystemEntry(origin, disposition))


def _queue_or_consume(
    item: _PendingEntry,
    pending: list[_PendingEntry],
    consumer: Callable[[_PendingEntry], None] | None,
) -> None:
    if consumer is None:
        pending.append(item)
    else:
        consumer(item)


def _account_inventory_warning(
    root: Path,
    inventory: GitFilesystemInventory,
    *,
    accounting: _AccountingBuilder,
    diagnostics: list[CollectorDiagnostic],
) -> None:
    if inventory.warning is None:
        return
    accounting.discover(root)
    accounting.record(root, RecordDisposition.RECORD_ERROR)
    diagnostics.append(_ignore_diagnostic(root, inventory.warning, warning=False))


def _account_inventory_ignored(
    root: Path,
    inventory: GitFilesystemInventory,
    *,
    seen: set[str],
    excluder: ExplicitExcluder,
    accounting: _AccountingBuilder,
) -> None:
    unseen = (
        inventory.ignored_relative_paths
        if not seen
        else tuple(path for path in inventory.ignored_relative_paths if path not in seen)
    )
    ignored_count = len(unseen)
    accounting.discover(root, ignored_count)
    if not excluder.patterns:
        accounting.record(root, RecordDisposition.IGNORED, ignored_count)
        return
    explicitly_excluded = sum(
        excluder.matches(
            relative_path,
            is_directory=relative_path in inventory.ignored_directory_paths,
        )
        for relative_path in unseen
    )
    accounting.record(root, RecordDisposition.EXPLICITLY_EXCLUDED, explicitly_excluded)
    accounting.record(root, RecordDisposition.IGNORED, ignored_count - explicitly_excluded)


def _entry_type(mode: int) -> EntryType | None:
    if stat.S_ISREG(mode):
        return EntryType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return EntryType.DIRECTORY
    if stat.S_ISLNK(mode):
        return EntryType.SYMLINK
    return None


def _is_semantic_git_admin(
    path: Path,
    repository: GitIgnoreRepository | None,
    *,
    relative_parts: tuple[str, ...] = (),
    admin_relative_parts: tuple[str, ...] = (),
) -> bool:
    # A physical admin extent inside the scan root is mapped once and then
    # compared lexically. Conventional markers retain the stricter shape check.
    if admin_relative_parts and relative_parts[: len(admin_relative_parts)] == admin_relative_parts:
        return True
    if not is_git_admin_name(path):
        return False
    return (repository is not None and is_within_git_admin(path, repository)) or is_git_admin_path(path)


def _repository_admin_relative_parts(
    root: Path,
    repository: GitIgnoreRepository | None,
) -> tuple[str, ...]:
    if repository is None or repository.admin_root is None:
        return ()
    try:
        physical_root = root.resolve(strict=True)
        physical_admin = repository.admin_root.resolve(strict=True)
        relative = physical_admin.relative_to(physical_root)
    except (OSError, RuntimeError, ValueError):
        return ()
    return PurePosixPath(relative.as_posix()).parts


def _entry_is_in_scope(
    entry_type: EntryType | None,
    *,
    include_regular_files: bool,
    include_directories: bool,
    include_symlinks: bool,
) -> bool:
    if entry_type is EntryType.REGULAR_FILE:
        return include_regular_files
    if entry_type is EntryType.DIRECTORY:
        return include_directories
    if entry_type is EntryType.SYMLINK:
        return include_symlinks
    return False


def _origin(root: Path, path: Path, entry_type: EntryType | None) -> RecordOrigin:
    type_name = entry_type.value if entry_type is not None else "special"
    return RecordOrigin(
        record_id=absolute_filesystem_entry_id(root, path, type_name),
        source=Source.FILESYSTEM,
        record_kind=RecordKind.FILESYSTEM_ENTRY,
        repository_or_root=root,
        path=path,
        entry_type=entry_type,
    )


def _record_key(root: Path) -> RecordCoverageKey:
    return RecordCoverageKey(Source.FILESYSTEM, os.fspath(root), RecordKind.FILESYSTEM_ENTRY)


def _timestamp_key(root: Path, kind: TimestampKind) -> TimestampCoverageKey:
    return TimestampCoverageKey(Source.FILESYSTEM, os.fspath(root), RecordKind.FILESYSTEM_ENTRY, kind)


def _is_lexical_descendant(path: Path, parent: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(parent)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(os.fspath(parent)) and os.path.normcase(
        os.fspath(path)
    ) != os.path.normcase(os.fspath(parent))


def _crosses_nested_repository(path: Path, parent: Path) -> bool:
    candidate = path
    while candidate != parent and _is_lexical_descendant(candidate, parent):
        if is_nested_repository_boundary(candidate, selected_root=parent):
            return True
        candidate = candidate.parent
    return False


def _stat_diagnostic(root: Path, path: Path, error: OSError, *, is_root: bool) -> CollectorDiagnostic:
    code = "path_not_found" if isinstance(error, FileNotFoundError) and is_root else "filesystem_stat_error"
    return CollectorDiagnostic(
        code=code,
        stage="filesystem_root_resolution" if is_root else "filesystem_stat",
        target=os.fspath(root),
        message=f"filesystem metadata could not be read: {error}",
        path=os.fspath(path),
    )


def _traversal_diagnostic(root: Path, path: Path, error: OSError) -> CollectorDiagnostic:
    return CollectorDiagnostic(
        code=(
            "filesystem_concurrent_mutation"
            if isinstance(error, DirectorySafetyError)
            else "filesystem_traversal_error"
        ),
        stage="filesystem_traversal",
        target=os.fspath(root),
        message=f"directory could not be fully enumerated: {error}",
        path=os.fspath(path),
    )


def _ignore_diagnostic(root: Path, error: Exception, *, warning: bool) -> CollectorDiagnostic:
    code = getattr(error, "code", "git_ignore_unavailable")
    hint = "Install/repair Git or use --include-ignored to request an unfiltered scan."
    if code == "git_filesystem_inventory_incomplete":
        hint = "Check the reported path permissions; the unreadable scope was not treated as complete."
    return CollectorDiagnostic(
        code=code,
        stage="filesystem_ignore_discovery",
        target=os.fspath(root),
        message=str(error),
        severity=DiagnosticSeverity.WARNING if warning else DiagnosticSeverity.ERROR,
        path=os.fspath(root),
        hint=hint,
    )


def _ignore_capability(
    root: Path,
    respect_gitignore: bool,
    probe: GitIgnoreProbe,
    *,
    error: Exception | None,
) -> Capability:
    if not respect_gitignore:
        status = CapabilityStatus.SUPPORTED
        note = "ignored entries were explicitly included"
    elif error is not None:
        status = CapabilityStatus.UNAVAILABLE
        note = str(error)
    elif probe.repository is None:
        status = CapabilityStatus.UNAVAILABLE
        note = probe.note
    else:
        status = CapabilityStatus.SUPPORTED
        note = probe.note
    return Capability(
        source=Source.FILESYSTEM,
        target=os.fspath(root),
        name="standard Git ignore semantics",
        status=status,
        note=note,
    )


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
