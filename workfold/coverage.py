"""Typed, reconcilable coverage accounting and operational diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypeVar

from workfold.models import RecordKind, Source, TimestampKind


class RecordDisposition(str, Enum):
    """Terminal outcomes for one discovered source record."""

    ELIGIBLE = "eligible"
    IGNORED = "ignored"
    EXPLICITLY_EXCLUDED = "explicitly_excluded"
    EXCLUDED_ENTRY_TYPE = "excluded_entry_type"
    SEMANTIC_GIT_ADMIN = "semantic_git_admin"
    RECORD_ERROR = "record_error"


class ExtractionDisposition(str, Enum):
    """Terminal extraction outcomes for one requested timestamp slot."""

    CAPTURED = "captured"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class SelectionDisposition(str, Enum):
    """Terminal filter outcomes for one captured observation."""

    INCLUDED = "included"
    OUTSIDE_DATE = "outside_date"
    IDENTITY_FILTERED = "identity_filtered"


class PlottingDisposition(str, Enum):
    """Terminal marker outcomes for one included observation."""

    MARKER = "marker"
    COALESCED_INTO_MARKER = "coalesced_into_marker"


class DiagnosticSeverity(str, Enum):
    """Machine-readable severity independent of rendered text."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticStage(str, Enum):
    """Pipeline stage that emitted a diagnostic."""

    CONFIGURATION = "configuration"
    DISCOVERY = "discovery"
    EXTRACTION = "extraction"
    SELECTION = "selection"
    PLOTTING = "plotting"
    RENDERING = "rendering"


class DiagnosticCode(str, Enum):
    """Stable diagnostic codes used by strict-mode and report policy."""

    INVALID_CONFIGURATION = "invalid_configuration"
    TARGET_UNAVAILABLE = "target_unavailable"
    TRAVERSAL_ERROR = "traversal_error"
    SUBPROCESS_ERROR = "subprocess_error"
    STAT_ERROR = "stat_error"
    DECODE_ERROR = "decode_error"
    PARSE_ERROR = "parse_error"
    CONCURRENT_MUTATION = "concurrent_mutation"
    REFLOG_UNAVAILABLE = "reflog_unavailable"
    INTERNAL_INVARIANT = "internal_invariant"


class CapabilityStatus(str, Enum):
    """Availability of a requested collector feature."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    POTENTIALLY_UNRELIABLE = "potentially_unreliable"


class CoverageStatus(str, Enum):
    """High-level result status derived from reconciled coverage."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A structured operational diagnostic; details remain unformatted."""

    code: DiagnosticCode
    stage: DiagnosticStage
    target: str
    severity: DiagnosticSeverity
    details: str
    path: Path | None = None
    provenance_id: str | None = None


@dataclass(frozen=True, slots=True)
class Capability:
    """A collector/platform capability statement."""

    source: Source
    target: str
    name: str
    status: CapabilityStatus
    timestamp_kind: TimestampKind | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RecordCoverageKey:
    """Partition key for record-discovery accounting."""

    source: Source
    target: str
    record_kind: RecordKind


@dataclass(frozen=True, slots=True)
class TimestampCoverageKey:
    """Partition key shared by extraction, selection, and plotting."""

    source: Source
    target: str
    record_kind: RecordKind
    timestamp_kind: TimestampKind

    def __post_init__(self) -> None:
        if self.timestamp_kind.source is not self.source:
            raise ValueError("timestamp kind does not belong to coverage source")


class CoverageInvariantError(RuntimeError):
    """Raised when a coverage partition violates a required equation."""


@dataclass(frozen=True, slots=True)
class RecordCoverage:
    """Reconciled discovery counts for one source/target/record partition."""

    key: RecordCoverageKey
    discovered: int = 0
    eligible: int = 0
    ignored: int = 0
    explicitly_excluded: int = 0
    excluded_entry_type: int = 0
    semantic_git_admin: int = 0
    record_errors: int = 0

    def __post_init__(self) -> None:
        _require_non_negative(
            self.discovered,
            self.eligible,
            self.ignored,
            self.explicitly_excluded,
            self.excluded_entry_type,
            self.semantic_git_admin,
            self.record_errors,
        )

    @property
    def terminal_total(self) -> int:
        """Return all mutually exclusive record dispositions."""

        return (
            self.eligible
            + self.ignored
            + self.explicitly_excluded
            + self.excluded_entry_type
            + self.semantic_git_admin
            + self.record_errors
        )

    def count(self, disposition: RecordDisposition) -> int:
        """Return the count for one typed disposition."""

        return {
            RecordDisposition.ELIGIBLE: self.eligible,
            RecordDisposition.IGNORED: self.ignored,
            RecordDisposition.EXPLICITLY_EXCLUDED: self.explicitly_excluded,
            RecordDisposition.EXCLUDED_ENTRY_TYPE: self.excluded_entry_type,
            RecordDisposition.SEMANTIC_GIT_ADMIN: self.semantic_git_admin,
            RecordDisposition.RECORD_ERROR: self.record_errors,
        }[disposition]

    def validate(self) -> None:
        """Assert the record-discovery equation for this partition."""

        if self.discovered != self.terminal_total:
            raise CoverageInvariantError(
                f"record coverage does not reconcile for {self.key!r}: "
                f"discovered={self.discovered}, dispositions={self.terminal_total}"
            )


@dataclass(frozen=True, slots=True)
class TimestampCoverage:
    """Reconciled extraction, selection, and plotting counts for one kind."""

    key: TimestampCoverageKey
    requested: int = 0
    captured: int = 0
    unavailable: int = 0
    unsupported: int = 0
    extraction_errors: int = 0
    included: int = 0
    outside_date: int = 0
    identity_filtered: int = 0
    markers: int = 0
    coalesced_into_markers: int = 0

    def __post_init__(self) -> None:
        _require_non_negative(
            self.requested,
            self.captured,
            self.unavailable,
            self.unsupported,
            self.extraction_errors,
            self.included,
            self.outside_date,
            self.identity_filtered,
            self.markers,
            self.coalesced_into_markers,
        )

    @property
    def extraction_total(self) -> int:
        """Return all extraction dispositions."""

        return self.captured + self.unavailable + self.unsupported + self.extraction_errors

    @property
    def selection_total(self) -> int:
        """Return all selection dispositions for captured observations."""

        return self.included + self.outside_date + self.identity_filtered

    @property
    def plotting_total(self) -> int:
        """Return all plotting dispositions for included observations."""

        return self.markers + self.coalesced_into_markers

    def extraction_count(self, disposition: ExtractionDisposition) -> int:
        """Return one extraction disposition count."""

        return {
            ExtractionDisposition.CAPTURED: self.captured,
            ExtractionDisposition.UNAVAILABLE: self.unavailable,
            ExtractionDisposition.UNSUPPORTED: self.unsupported,
            ExtractionDisposition.ERROR: self.extraction_errors,
        }[disposition]

    def selection_count(self, disposition: SelectionDisposition) -> int:
        """Return one selection disposition count."""

        return {
            SelectionDisposition.INCLUDED: self.included,
            SelectionDisposition.OUTSIDE_DATE: self.outside_date,
            SelectionDisposition.IDENTITY_FILTERED: self.identity_filtered,
        }[disposition]

    def plotting_count(self, disposition: PlottingDisposition) -> int:
        """Return one plotting disposition count."""

        return {
            PlottingDisposition.MARKER: self.markers,
            PlottingDisposition.COALESCED_INTO_MARKER: self.coalesced_into_markers,
        }[disposition]

    def validate(self) -> None:
        """Assert the extraction, selection, and plotting equations."""

        problems: list[str] = []
        if self.requested != self.extraction_total:
            problems.append(f"requested={self.requested}, extraction dispositions={self.extraction_total}")
        if self.captured != self.selection_total:
            problems.append(f"captured={self.captured}, selection dispositions={self.selection_total}")
        if self.included != self.plotting_total:
            problems.append(f"included={self.included}, plotting dispositions={self.plotting_total}")
        if problems:
            raise CoverageInvariantError(
                f"timestamp coverage does not reconcile for {self.key!r}: {'; '.join(problems)}"
            )


@dataclass(frozen=True, slots=True)
class CoverageLedger:
    """An immutable, validated snapshot of all coverage partitions."""

    records: tuple[RecordCoverage, ...] = ()
    timestamps: tuple[TimestampCoverage, ...] = ()

    def __post_init__(self) -> None:
        if len({item.key for item in self.records}) != len(self.records):
            raise ValueError("record coverage contains duplicate partition keys")
        if len({item.key for item in self.timestamps}) != len(self.timestamps):
            raise ValueError("timestamp coverage contains duplicate partition keys")

    def validate(self) -> None:
        """Assert all three required equations in every partition."""

        for item in self.records:
            item.validate()
        for item in self.timestamps:
            item.validate()
        records_by_key = {item.key: item for item in self.records}
        for item in self.timestamps:
            record_key = RecordCoverageKey(
                item.key.source,
                item.key.target,
                item.key.record_kind,
            )
            record = records_by_key.get(record_key)
            if record is None:
                raise CoverageInvariantError(f"timestamp coverage has no matching record partition for {item.key!r}")
            if item.requested != record.eligible:
                raise CoverageInvariantError(
                    f"timestamp slots do not match eligible records for {item.key!r}: "
                    f"requested={item.requested}, eligible={record.eligible}"
                )

    def merge(self, *others: CoverageLedger) -> CoverageLedger:
        """Add independent ledger partitions and return a reconciled ledger."""

        builder = CoverageLedgerBuilder()
        builder.add_ledger(self)
        for other in others:
            builder.add_ledger(other)
        return builder.build()

    @property
    def records_discovered(self) -> int:
        """Return total records discovered across requested partitions."""

        return sum(item.discovered for item in self.records)

    @property
    def slots_requested(self) -> int:
        """Return total timestamp slots requested."""

        return sum(item.requested for item in self.timestamps)

    @property
    def observations_captured(self) -> int:
        """Return total atomic observations captured."""

        return sum(item.captured for item in self.timestamps)

    @property
    def observations_included(self) -> int:
        """Return total atomic observations included after filters."""

        return sum(item.included for item in self.timestamps)

    @property
    def markers_plotted(self) -> int:
        """Return total activity markers after authorized coalescing."""

        return sum(item.markers for item in self.timestamps)

    @property
    def has_operational_errors(self) -> bool:
        """Return whether record or extraction failures made coverage partial."""

        return any(item.record_errors for item in self.records) or any(
            item.extraction_errors for item in self.timestamps
        )


@dataclass(slots=True)
class CoverageLedgerBuilder:
    """Mutable collection-time builder that emits an immutable ledger."""

    _records_discovered: dict[RecordCoverageKey, int] = field(default_factory=lambda: {})
    _record_outcomes: dict[tuple[RecordCoverageKey, RecordDisposition], int] = field(default_factory=lambda: {})
    _slots_requested: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _extraction_outcomes: dict[tuple[TimestampCoverageKey, ExtractionDisposition], int] = field(
        default_factory=lambda: {}
    )
    _selection_outcomes: dict[tuple[TimestampCoverageKey, SelectionDisposition], int] = field(
        default_factory=lambda: {}
    )
    _plotting_outcomes: dict[tuple[TimestampCoverageKey, PlottingDisposition], int] = field(default_factory=lambda: {})

    def discover_record(self, key: RecordCoverageKey, count: int = 1) -> None:
        """Record how many source records were discovered."""

        _increment(self._records_discovered, key, count)

    def record_outcome(self, key: RecordCoverageKey, disposition: RecordDisposition, count: int = 1) -> None:
        """Assign discovered records to one terminal disposition."""

        _increment(self._record_outcomes, (key, disposition), count)

    def request_slot(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record how many timestamp slots were requested."""

        _increment(self._slots_requested, key, count)

    def extraction_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: ExtractionDisposition,
        count: int = 1,
    ) -> None:
        """Assign requested slots to one terminal extraction disposition."""

        _increment(self._extraction_outcomes, (key, disposition), count)

    def selection_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: SelectionDisposition,
        count: int = 1,
    ) -> None:
        """Assign captured observations to one terminal filter disposition."""

        _increment(self._selection_outcomes, (key, disposition), count)

    def plotting_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: PlottingDisposition,
        count: int = 1,
    ) -> None:
        """Assign included observations to a marker/coalescing disposition."""

        _increment(self._plotting_outcomes, (key, disposition), count)

    def add_ledger(self, ledger: CoverageLedger) -> None:
        """Add all counts from an immutable ledger snapshot."""

        for item in ledger.records:
            self.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                self.record_outcome(item.key, disposition, item.count(disposition))
        for item in ledger.timestamps:
            self.request_slot(item.key, item.requested)
            for disposition in ExtractionDisposition:
                self.extraction_outcome(item.key, disposition, item.extraction_count(disposition))
            for disposition in SelectionDisposition:
                self.selection_outcome(item.key, disposition, item.selection_count(disposition))
            for disposition in PlottingDisposition:
                self.plotting_outcome(item.key, disposition, item.plotting_count(disposition))

    def build(self, *, validate: bool = True) -> CoverageLedger:
        """Create a deterministic immutable snapshot, validating by default."""

        record_keys = set(self._records_discovered)
        record_keys.update(key for key, _ in self._record_outcomes)
        timestamp_keys = set(self._slots_requested)
        timestamp_keys.update(key for key, _ in self._extraction_outcomes)
        timestamp_keys.update(key for key, _ in self._selection_outcomes)
        timestamp_keys.update(key for key, _ in self._plotting_outcomes)

        records = tuple(
            RecordCoverage(
                key,
                self._records_discovered.get(key, 0),
                *tuple(self._record_outcomes.get((key, disposition), 0) for disposition in RecordDisposition),
            )
            for key in sorted(record_keys, key=_record_key_sort)
        )
        timestamps = tuple(
            TimestampCoverage(
                key,
                self._slots_requested.get(key, 0),
                *tuple(self._extraction_outcomes.get((key, disposition), 0) for disposition in ExtractionDisposition),
                *tuple(self._selection_outcomes.get((key, disposition), 0) for disposition in SelectionDisposition),
                *tuple(self._plotting_outcomes.get((key, disposition), 0) for disposition in PlottingDisposition),
            )
            for key in sorted(timestamp_keys, key=_timestamp_key_sort)
        )
        ledger = CoverageLedger(records, timestamps)
        if validate:
            ledger.validate()
        return ledger


def coverage_status(
    ledger: CoverageLedger,
    diagnostics: tuple[Diagnostic, ...] = (),
    *,
    any_collector_succeeded: bool = True,
) -> CoverageStatus:
    """Derive report status from typed counts and diagnostics."""

    ledger.validate()
    if not any_collector_succeeded:
        return CoverageStatus.FAILED
    if ledger.has_operational_errors or any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return CoverageStatus.PARTIAL
    return CoverageStatus.COMPLETE


_Key = TypeVar("_Key")


def _increment(counts: dict[_Key, int], key: _Key, count: int) -> None:
    if count < 0:
        raise ValueError("coverage increments must be non-negative")
    counts[key] = counts.get(key, 0) + count


def _require_non_negative(*values: int) -> None:
    if any(value < 0 for value in values):
        raise ValueError("coverage counts must be non-negative")


def _record_key_sort(key: RecordCoverageKey) -> tuple[str, str, str]:
    return key.source.value, key.target, key.record_kind.value


def _timestamp_key_sort(key: TimestampCoverageKey) -> tuple[str, str, str, str]:
    return key.source.value, key.target, key.record_kind.value, key.timestamp_kind.value


__all__ = [
    "Capability",
    "CapabilityStatus",
    "CoverageInvariantError",
    "CoverageLedger",
    "CoverageLedgerBuilder",
    "CoverageStatus",
    "Diagnostic",
    "DiagnosticCode",
    "DiagnosticSeverity",
    "DiagnosticStage",
    "ExtractionDisposition",
    "PlottingDisposition",
    "RecordCoverage",
    "RecordCoverageKey",
    "RecordDisposition",
    "SelectionDisposition",
    "TimestampCoverage",
    "TimestampCoverageKey",
    "coverage_status",
]
