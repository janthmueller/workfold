"""Immutable filesystem collection results and coverage models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, DiagnosticSeverity
from workfold.coverage import (
    Capability,
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
from workfold.models import RecordOrigin, TimestampObservation


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
        """Complete the ledger after common date selection and marker creation."""

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
                builder.selection_outcome(item.key, disposition, selection_counts.get((item.key, disposition), 0))
            for disposition in PlottingDisposition:
                builder.plotting_outcome(item.key, disposition, plotting_counts.get((item.key, disposition), 0))
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

        return any(item.severity is DiagnosticSeverity.ERROR or item.affects_completeness for item in self.diagnostics)

    def build_coverage(
        self,
        selection_by_observation_id: Mapping[str, SelectionDisposition],
        plotting_by_observation_id: Mapping[str, PlottingDisposition],
    ) -> CoverageLedger:
        return self.accounting.build_coverage(selection_by_observation_id, plotting_by_observation_id)

    def build_coverage_counts(
        self,
        selection_counts: Mapping[tuple[TimestampCoverageKey, SelectionDisposition], int],
        plotting_counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    ) -> CoverageLedger:
        return self.accounting.build_coverage_counts(selection_counts, plotting_counts)
