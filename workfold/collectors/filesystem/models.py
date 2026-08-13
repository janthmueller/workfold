"""Immutable filesystem collection results and coverage models."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, diagnostics_are_partial
from workfold.coverage import (
    Capability,
    CoverageLedger,
    CoverageLedgerBuilder,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverageKey,
)
from workfold.models import RecordOrigin, TimestampObservation


@dataclass(frozen=True, slots=True)
class TimestampExtractionCoverage:
    """Extraction and pre-normalization scope accounting for one timestamp kind."""

    key: TimestampCoverageKey
    requested: int
    captured: int
    unavailable: int
    unsupported: int
    errors: int
    scope_matches: int
    scope_match_ids: tuple[str, ...]
    scope_match_ids_complete: bool = True

    def __post_init__(self) -> None:
        values = (
            self.requested,
            self.captured,
            self.unavailable,
            self.unsupported,
            self.errors,
            self.scope_matches,
        )
        if any(value < 0 for value in values):
            raise ValueError("filesystem extraction counts must be non-negative")
        if self.requested != self.captured + self.unavailable + self.unsupported + self.errors:
            raise ValueError("filesystem extraction accounting does not reconcile")
        if self.scope_match_ids_complete and self.scope_matches != len(self.scope_match_ids):
            raise ValueError("scope-match count must match the retained observation identities")
        if len(self.scope_match_ids) > self.scope_matches:
            raise ValueError("retained observation identities cannot exceed scope matches")
        if self.scope_matches > self.captured:
            raise ValueError("filesystem scope matches cannot exceed captured timestamps")
        if len(set(self.scope_match_ids)) != len(self.scope_match_ids):
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
    """Reconciled discovery/extraction counts independent of result plotting."""

    records: tuple[RecordCoverage, ...]
    timestamps: tuple[TimestampExtractionCoverage, ...]
    pruned_ignored_subtrees: int = 0

    def __post_init__(self) -> None:
        if self.pruned_ignored_subtrees < 0:
            raise ValueError("pruned ignored subtree count must be non-negative")
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
        selected_observation_ids: Set[str],
        plotting_by_observation_id: Mapping[str, PlottingDisposition],
    ) -> CoverageLedger:
        """Complete the ledger after scope selection and marker creation."""

        if any(not item.scope_match_ids_complete for item in self.timestamps):
            raise ValueError("observation-ID coverage is unavailable for a non-retaining collection")
        expected = {observation_id for item in self.timestamps for observation_id in item.scope_match_ids}
        selected = set(selected_observation_ids)
        unknown = selected - expected
        if unknown:
            raise ValueError(f"selected observation set contains {len(unknown)} unknown filesystem observations")
        missing = expected - selected
        if missing:
            raise ValueError(f"selected observation set omits {len(missing)} matched filesystem observations")
        plotted = set(plotting_by_observation_id)
        if plotted != selected:
            missing = len(selected - plotted)
            extra = len(plotted - selected)
            raise ValueError(
                f"plotting map must cover every selected filesystem observation (missing={missing}, extra={extra})"
            )

        builder = CoverageLedgerBuilder()
        for item in self.records:
            builder.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                builder.record_outcome(item.key, disposition, item.count(disposition))
        for item in self.timestamps:
            builder.examine_slot(item.key, item.requested)
            for disposition in ExtractionDisposition:
                builder.extraction_outcome(item.key, disposition, item.count(disposition))
            builder.match_scope(item.key, item.scope_matches)
            for observation_id in item.scope_match_ids:
                if observation_id in selected:
                    builder.select_observation(item.key)
                    builder.plotting_outcome(item.key, plotting_by_observation_id[observation_id])
        return builder.build()

    def build_coverage_counts(
        self,
        observation_counts: Mapping[TimestampCoverageKey, int],
        plotting_counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    ) -> CoverageLedger:
        """Complete coverage from partition counts without retaining observation IDs."""

        builder = CoverageLedgerBuilder()
        for item in self.records:
            builder.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                builder.record_outcome(item.key, disposition, item.count(disposition))
        for item in self.timestamps:
            builder.examine_slot(item.key, item.requested)
            for disposition in ExtractionDisposition:
                builder.extraction_outcome(item.key, disposition, item.count(disposition))
            builder.match_scope(item.key, item.scope_matches)
            builder.select_observation(item.key, observation_counts.get(item.key, 0))
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

        return diagnostics_are_partial(self.diagnostics)

    def build_coverage(
        self,
        selected_observation_ids: Set[str],
        plotting_by_observation_id: Mapping[str, PlottingDisposition],
    ) -> CoverageLedger:
        return self.accounting.build_coverage(selected_observation_ids, plotting_by_observation_id)

    def build_coverage_counts(
        self,
        observation_counts: Mapping[TimestampCoverageKey, int],
        plotting_counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    ) -> CoverageLedger:
        return self.accounting.build_coverage_counts(observation_counts, plotting_counts)
