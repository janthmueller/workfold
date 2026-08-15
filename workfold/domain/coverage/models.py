"""Immutable coverage partitions, capabilities, and invariants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from workfold.domain.evidence import EvidenceKind
from workfold.domain.observations import EntryType, RecordKind, Source, TimestampKind


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


class CapabilityKind(str, Enum):
    """Stable semantic identity for a collector/platform capability."""

    FS_CREATED_TIME = "fs_created_time"
    FS_MODIFIED_TIME = "fs_modified_time"
    FS_METADATA_CHANGED_TIME = "fs_metadata_changed_time"
    FS_ACCESSED_TIME = "fs_accessed_time"
    GIT_IGNORE_SEMANTICS = "git_ignore_semantics"


@dataclass(frozen=True, slots=True)
class Capability:
    """A collector/platform capability statement."""

    source: Source
    target: str
    kind: CapabilityKind
    name: str
    status: CapabilityStatus
    timestamp_kind: TimestampKind | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.source is not Source.FILESYSTEM:
            raise ValueError("defined capability kinds currently belong to the filesystem source")
        timestamp_capabilities = {
            CapabilityKind.FS_CREATED_TIME: TimestampKind.FS_CREATED,
            CapabilityKind.FS_MODIFIED_TIME: TimestampKind.FS_MODIFIED,
            CapabilityKind.FS_METADATA_CHANGED_TIME: TimestampKind.FS_METADATA_CHANGED,
            CapabilityKind.FS_ACCESSED_TIME: TimestampKind.FS_ACCESSED,
        }
        expected_timestamp = timestamp_capabilities.get(self.kind)
        if expected_timestamp is not None and self.timestamp_kind is not expected_timestamp:
            raise ValueError("timestamp capability kind does not match its timestamp kind")
        if self.kind is CapabilityKind.GIT_IGNORE_SEMANTICS and self.timestamp_kind is not None:
            raise ValueError("Git ignore capability cannot describe a timestamp kind")


@dataclass(frozen=True, slots=True)
class RecordCoverageKey:
    """Partition key for record-discovery accounting."""

    source: Source
    target: str
    record_kind: RecordKind

    def __post_init__(self) -> None:
        filesystem_record = self.record_kind is RecordKind.FILESYSTEM_ENTRY
        if filesystem_record != (self.source is Source.FILESYSTEM):
            raise ValueError("record kind does not belong to coverage source")


@dataclass(frozen=True, slots=True)
class TimestampCoverageKey:
    """Partition key shared by extraction and selected-result accounting."""

    source: Source
    target: str
    record_kind: RecordKind
    timestamp_kind: TimestampKind
    entry_type: EntryType | None = None

    def __post_init__(self) -> None:
        if self.timestamp_kind.source is not self.source:
            raise ValueError("timestamp kind does not belong to coverage source")
        if self.record_kind is RecordKind.FILESYSTEM_ENTRY and self.entry_type is None:
            raise ValueError("filesystem timestamp coverage requires an entry type")
        if self.record_kind is not RecordKind.FILESYSTEM_ENTRY and self.entry_type is not None:
            raise ValueError("entry type is valid only for filesystem timestamp coverage")
        try:
            EvidenceKind.from_dimensions(self.record_kind, self.timestamp_kind, self.entry_type)
        except ValueError as error:
            raise ValueError("timestamp kind does not belong to coverage record kind") from error


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
    materialization. ``scope_errors`` records readable values whose remaining
    metadata could not be obtained to finish selection. ``selected`` is
    recorded independently when the pipeline receives matching observations.
    These bridges detect silent loss without retaining every out-of-scope
    timestamp in memory.
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
    scope_errors: int = 0

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
            self.scope_errors,
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
        if self.scope_matches + self.scope_errors > self.values_read:
            problems.append(f"scope outcomes={self.scope_matches + self.scope_errors}, values read={self.values_read}")
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
class CollectionTimestampCoverage:
    """Collector-owned accounting before pipeline selection and plotting."""

    key: TimestampCoverageKey
    examined: int = 0
    values_read: int = 0
    unavailable: int = 0
    unsupported: int = 0
    extraction_errors: int = 0
    scope_matches: int = 0
    materialization_errors: int = 0
    scope_errors: int = 0

    def __post_init__(self) -> None:
        _require_non_negative(
            self.examined,
            self.values_read,
            self.unavailable,
            self.unsupported,
            self.extraction_errors,
            self.scope_matches,
            self.materialization_errors,
            self.scope_errors,
        )

    @property
    def extraction_total(self) -> int:
        return self.values_read + self.unavailable + self.unsupported + self.extraction_errors

    def extraction_count(self, disposition: ExtractionDisposition) -> int:
        return {
            ExtractionDisposition.CAPTURED: self.values_read,
            ExtractionDisposition.UNAVAILABLE: self.unavailable,
            ExtractionDisposition.UNSUPPORTED: self.unsupported,
            ExtractionDisposition.ERROR: self.extraction_errors,
        }[disposition]

    def validate(self) -> None:
        problems: list[str] = []
        if self.examined != self.extraction_total:
            problems.append(f"examined={self.examined}, extraction dispositions={self.extraction_total}")
        if self.scope_matches + self.scope_errors > self.values_read:
            problems.append(f"scope outcomes={self.scope_matches + self.scope_errors}, values read={self.values_read}")
        if self.materialization_errors > self.scope_matches:
            problems.append(
                f"materialization errors={self.materialization_errors}, scope matches={self.scope_matches}"
            )
        if problems:
            raise CoverageInvariantError(
                f"collection timestamp coverage does not reconcile for {self.key!r}: {'; '.join(problems)}"
            )


@dataclass(frozen=True, slots=True)
class CoverageFragment:
    """One source's immutable coverage contribution before the shared pipeline."""

    records: tuple[RecordCoverage, ...] = ()
    timestamps: tuple[CollectionTimestampCoverage, ...] = ()

    def __post_init__(self) -> None:
        if len({item.key for item in self.records}) != len(self.records):
            raise ValueError("coverage fragment contains duplicate record partition keys")
        if len({item.key for item in self.timestamps}) != len(self.timestamps):
            raise ValueError("coverage fragment contains duplicate timestamp partition keys")
        records_by_key = {item.key for item in self.records}
        for item in self.records:
            item.validate()
        for item in self.timestamps:
            item.validate()
            record_key = RecordCoverageKey(item.key.source, item.key.target, item.key.record_kind)
            if record_key not in records_by_key:
                raise CoverageInvariantError(
                    f"coverage fragment timestamp has no matching record partition for {item.key!r}"
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
            if record_key not in records_by_key:
                raise CoverageInvariantError(f"timestamp coverage has no matching record partition for {item.key!r}")

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
            item.extraction_errors or item.materialization_errors or item.scope_errors for item in self.timestamps
        )


def _require_non_negative(*values: int) -> None:
    if any(value < 0 for value in values):
        raise ValueError("coverage counts must be non-negative")


__all__ = [
    "Capability",
    "CapabilityKind",
    "CapabilityStatus",
    "CollectionTimestampCoverage",
    "CoverageFragment",
    "CoverageInvariantError",
    "CoverageLedger",
    "ExtractionDisposition",
    "PlottingDisposition",
    "RecordCoverage",
    "RecordCoverageKey",
    "RecordDisposition",
    "TimestampCoverage",
    "TimestampCoverageKey",
]
