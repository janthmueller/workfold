"""Typed, reconcilable coverage accounting."""

from workfold.domain.coverage.builder import CoverageLedgerBuilder, finalize_coverage_fragments, merge_ledgers
from workfold.domain.coverage.models import (
    Capability,
    CapabilityKind,
    CapabilityReason,
    CapabilityStatus,
    CollectionTimestampCoverage,
    CoverageFragment,
    CoverageInvariantError,
    CoverageLedger,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverage,
    TimestampCoverageKey,
)

__all__ = [
    "Capability",
    "CapabilityKind",
    "CapabilityReason",
    "CapabilityStatus",
    "CollectionTimestampCoverage",
    "CoverageFragment",
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
    "finalize_coverage_fragments",
    "merge_ledgers",
]
