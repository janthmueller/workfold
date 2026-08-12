"""Typed, reconcilable coverage accounting and operational diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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


class PlottingDisposition(str, Enum):
    """Terminal marker outcomes for one selected observation."""

    MARKER = "marker"
    COALESCED_INTO_MARKER = "coalesced_into_marker"


class CapabilityStatus(str, Enum):
    """Availability of a requested collector feature."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    POTENTIALLY_UNRELIABLE = "potentially_unreliable"


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
    """Partition key shared by extraction and selected-result accounting."""

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
    """Reconciled extraction, scope, materialization, and plotting counts.

    Extraction counters describe timestamp slots a collector examined.
    ``scope_matches`` is recorded by each source collector before consumer
    delivery; early-selection paths record it before rich observation
    materialization. ``selected`` is recorded independently when the pipeline
    receives those observations. This bridge detects silent loss without
    retaining every out-of-scope timestamp in memory.
    """

    key: TimestampCoverageKey
    examined: int = 0
    values_read: int = 0
    unavailable: int = 0
    unsupported: int = 0
    extraction_errors: int = 0
    scope_matches: int = 0
    materialization_errors: int = 0
    selected: int = 0
    markers: int = 0
    coalesced_into_markers: int = 0

    def __post_init__(self) -> None:
        _require_non_negative(
            self.examined,
            self.values_read,
            self.unavailable,
            self.unsupported,
            self.extraction_errors,
            self.scope_matches,
            self.materialization_errors,
            self.selected,
            self.markers,
            self.coalesced_into_markers,
        )

    @property
    def extraction_total(self) -> int:
        """Return all extraction dispositions."""

        return self.values_read + self.unavailable + self.unsupported + self.extraction_errors

    @property
    def plotting_total(self) -> int:
        """Return all plotting dispositions for selected observations."""

        return self.markers + self.coalesced_into_markers

    def extraction_count(self, disposition: ExtractionDisposition) -> int:
        """Return one extraction disposition count."""

        return {
            ExtractionDisposition.CAPTURED: self.values_read,
            ExtractionDisposition.UNAVAILABLE: self.unavailable,
            ExtractionDisposition.UNSUPPORTED: self.unsupported,
            ExtractionDisposition.ERROR: self.extraction_errors,
        }[disposition]

    def plotting_count(self, disposition: PlottingDisposition) -> int:
        """Return one plotting disposition count."""

        return {
            PlottingDisposition.MARKER: self.markers,
            PlottingDisposition.COALESCED_INTO_MARKER: self.coalesced_into_markers,
        }[disposition]

    def validate(self) -> None:
        """Assert extraction and selected-result conservation."""

        problems: list[str] = []
        if self.examined != self.extraction_total:
            problems.append(f"examined={self.examined}, extraction dispositions={self.extraction_total}")
        if self.scope_matches > self.values_read:
            problems.append(f"scope matches={self.scope_matches}, values read={self.values_read}")
        if self.scope_matches != self.selected + self.materialization_errors:
            problems.append(
                f"scope matches={self.scope_matches}, selected plus materialization errors="
                f"{self.selected + self.materialization_errors}"
            )
        if self.selected != self.plotting_total:
            problems.append(f"selected={self.selected}, plotting dispositions={self.plotting_total}")
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
        """Assert collector-local and selected-result equations."""

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
    def slots_examined(self) -> int:
        """Return total timestamp slots examined by collectors."""

        return sum(item.examined for item in self.timestamps)

    @property
    def timestamp_values_read(self) -> int:
        """Return total timestamp values read by collectors."""

        return sum(item.values_read for item in self.timestamps)

    @property
    def observations_selected(self) -> int:
        """Return total atomic observations selected by the requested scope."""

        return sum(item.selected for item in self.timestamps)

    @property
    def timestamp_values_matching_scope(self) -> int:
        """Return readable timestamp values matching the requested scope."""

        return sum(item.scope_matches for item in self.timestamps)

    @property
    def markers_plotted(self) -> int:
        """Return total activity markers after authorized coalescing."""

        return sum(item.markers for item in self.timestamps)

    @property
    def has_operational_errors(self) -> bool:
        """Return whether record or extraction failures made coverage partial."""

        return any(item.record_errors for item in self.records) or any(
            item.extraction_errors or item.materialization_errors for item in self.timestamps
        )


@dataclass(slots=True)
class CoverageLedgerBuilder:
    """Mutable collection-time builder that emits an immutable ledger."""

    _records_discovered: dict[RecordCoverageKey, int] = field(default_factory=lambda: {})
    _record_outcomes: dict[tuple[RecordCoverageKey, RecordDisposition], int] = field(default_factory=lambda: {})
    _slots_examined: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _extraction_outcomes: dict[tuple[TimestampCoverageKey, ExtractionDisposition], int] = field(
        default_factory=lambda: {}
    )
    _scope_matches: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _materialization_errors: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _selected_observations: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _plotting_outcomes: dict[tuple[TimestampCoverageKey, PlottingDisposition], int] = field(default_factory=lambda: {})

    def discover_record(self, key: RecordCoverageKey, count: int = 1) -> None:
        """Record how many source records were discovered."""

        _increment(self._records_discovered, key, count)

    def record_outcome(self, key: RecordCoverageKey, disposition: RecordDisposition, count: int = 1) -> None:
        """Assign discovered records to one terminal disposition."""

        _increment(self._record_outcomes, (key, disposition), count)

    def examine_slot(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record how many timestamp slots a collector examined."""

        _increment(self._slots_examined, key, count)

    def extraction_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: ExtractionDisposition,
        count: int = 1,
    ) -> None:
        """Assign examined slots to one terminal extraction disposition."""

        _increment(self._extraction_outcomes, (key, disposition), count)

    def select_observation(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record observations that matched the requested query scope."""

        _increment(self._selected_observations, key, count)

    def match_scope(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record readable timestamp values matching the requested scope."""

        _increment(self._scope_matches, key, count)

    def materialization_error(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record matching values that could not become observations."""

        _increment(self._materialization_errors, key, count)

    def plotting_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: PlottingDisposition,
        count: int = 1,
    ) -> None:
        """Assign selected observations to a marker/coalescing disposition."""

        _increment(self._plotting_outcomes, (key, disposition), count)

    def add_ledger(self, ledger: CoverageLedger) -> None:
        """Add all counts from an immutable ledger snapshot."""

        for item in ledger.records:
            self.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                self.record_outcome(item.key, disposition, item.count(disposition))
        for item in ledger.timestamps:
            self.examine_slot(item.key, item.examined)
            for disposition in ExtractionDisposition:
                self.extraction_outcome(item.key, disposition, item.extraction_count(disposition))
            self.match_scope(item.key, item.scope_matches)
            self.materialization_error(item.key, item.materialization_errors)
            self.select_observation(item.key, item.selected)
            for disposition in PlottingDisposition:
                self.plotting_outcome(item.key, disposition, item.plotting_count(disposition))

    def build(self, *, validate: bool = True) -> CoverageLedger:
        """Create a deterministic immutable snapshot, validating by default."""

        record_keys = set(self._records_discovered)
        record_keys.update(key for key, _ in self._record_outcomes)
        timestamp_keys = set(self._slots_examined)
        timestamp_keys.update(key for key, _ in self._extraction_outcomes)
        timestamp_keys.update(self._scope_matches)
        timestamp_keys.update(self._materialization_errors)
        timestamp_keys.update(self._selected_observations)
        timestamp_keys.update(key for key, _ in self._plotting_outcomes)

        records = tuple(
            RecordCoverage(
                key=key,
                discovered=self._records_discovered.get(key, 0),
                eligible=self._record_outcomes.get((key, RecordDisposition.ELIGIBLE), 0),
                ignored=self._record_outcomes.get((key, RecordDisposition.IGNORED), 0),
                explicitly_excluded=self._record_outcomes.get((key, RecordDisposition.EXPLICITLY_EXCLUDED), 0),
                excluded_entry_type=self._record_outcomes.get((key, RecordDisposition.EXCLUDED_ENTRY_TYPE), 0),
                semantic_git_admin=self._record_outcomes.get((key, RecordDisposition.SEMANTIC_GIT_ADMIN), 0),
                record_errors=self._record_outcomes.get((key, RecordDisposition.RECORD_ERROR), 0),
            )
            for key in sorted(record_keys, key=_record_key_sort)
        )
        timestamps = tuple(
            TimestampCoverage(
                key=key,
                examined=self._slots_examined.get(key, 0),
                values_read=self._extraction_outcomes.get((key, ExtractionDisposition.CAPTURED), 0),
                unavailable=self._extraction_outcomes.get((key, ExtractionDisposition.UNAVAILABLE), 0),
                unsupported=self._extraction_outcomes.get((key, ExtractionDisposition.UNSUPPORTED), 0),
                extraction_errors=self._extraction_outcomes.get((key, ExtractionDisposition.ERROR), 0),
                scope_matches=self._scope_matches.get(key, 0),
                materialization_errors=self._materialization_errors.get(key, 0),
                selected=self._selected_observations.get(key, 0),
                markers=self._plotting_outcomes.get((key, PlottingDisposition.MARKER), 0),
                coalesced_into_markers=self._plotting_outcomes.get((key, PlottingDisposition.COALESCED_INTO_MARKER), 0),
            )
            for key in sorted(timestamp_keys, key=_timestamp_key_sort)
        )
        ledger = CoverageLedger(records, timestamps)
        if validate:
            ledger.validate()
        return ledger


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
    "ExtractionDisposition",
    "PlottingDisposition",
    "RecordCoverage",
    "RecordCoverageKey",
    "RecordDisposition",
    "TimestampCoverage",
    "TimestampCoverageKey",
]
