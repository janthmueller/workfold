from __future__ import annotations

from pathlib import Path

import pytest
from workfold.coverage import (
    Capability,
    CapabilityStatus,
    CoverageInvariantError,
    CoverageLedger,
    CoverageLedgerBuilder,
    CoverageStatus,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    DiagnosticStage,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    SelectionDisposition,
    TimestampCoverage,
    TimestampCoverageKey,
    coverage_status,
)
from workfold.models import RecordKind, Source, TimestampKind

RECORD_KEY = RecordCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT)
AUTHOR_KEY = TimestampCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT, TimestampKind.GIT_AUTHOR)


def _complete_builder() -> CoverageLedgerBuilder:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, 3)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, 2)
    builder.record_outcome(RECORD_KEY, RecordDisposition.IGNORED, 1)
    builder.request_slot(AUTHOR_KEY, 2)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.CAPTURED, 1)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.UNAVAILABLE, 1)
    builder.selection_outcome(AUTHOR_KEY, SelectionDisposition.INCLUDED, 1)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.MARKER, 1)
    return builder


def test_ledger_builder_reconciles_all_three_phases() -> None:
    ledger = _complete_builder().build()
    record = ledger.records[0]
    timestamp = ledger.timestamps[0]

    assert record.count(RecordDisposition.ELIGIBLE) == 2
    assert record.terminal_total == 3
    assert timestamp.extraction_count(ExtractionDisposition.UNAVAILABLE) == 1
    assert timestamp.selection_count(SelectionDisposition.INCLUDED) == 1
    assert timestamp.plotting_count(PlottingDisposition.MARKER) == 1
    assert timestamp.extraction_total == timestamp.requested == 2
    assert timestamp.selection_total == timestamp.captured == 1
    assert timestamp.plotting_total == timestamp.included == 1
    assert ledger.records_discovered == 3
    assert ledger.slots_requested == 2
    assert ledger.observations_captured == 1
    assert ledger.observations_included == 1
    assert ledger.markers_plotted == 1
    assert not ledger.has_operational_errors
    assert coverage_status(ledger) is CoverageStatus.COMPLETE


def test_every_record_disposition_has_a_typed_counter() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, len(RecordDisposition))
    for disposition in RecordDisposition:
        builder.record_outcome(RECORD_KEY, disposition)
    ledger = builder.build()
    assert all(ledger.records[0].count(disposition) == 1 for disposition in RecordDisposition)
    assert ledger.has_operational_errors


def test_every_timestamp_disposition_has_a_typed_counter() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, len(ExtractionDisposition) + 4)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, len(ExtractionDisposition) + 4)
    builder.request_slot(AUTHOR_KEY, len(ExtractionDisposition) + 4)
    for disposition in ExtractionDisposition:
        builder.extraction_outcome(
            AUTHOR_KEY,
            disposition,
            5 if disposition is ExtractionDisposition.CAPTURED else 1,
        )
    builder.selection_outcome(AUTHOR_KEY, SelectionDisposition.INCLUDED, 3)
    builder.selection_outcome(AUTHOR_KEY, SelectionDisposition.OUTSIDE_DATE)
    builder.selection_outcome(AUTHOR_KEY, SelectionDisposition.IDENTITY_FILTERED)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.MARKER, 2)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.COALESCED_INTO_MARKER)
    coverage = builder.build().timestamps[0]
    assert coverage.extraction_count(ExtractionDisposition.CAPTURED) == 5
    assert all(
        coverage.extraction_count(item) == 1
        for item in ExtractionDisposition
        if item is not ExtractionDisposition.CAPTURED
    )
    assert coverage.selection_count(SelectionDisposition.INCLUDED) == 3
    assert coverage.selection_count(SelectionDisposition.OUTSIDE_DATE) == 1
    assert coverage.selection_count(SelectionDisposition.IDENTITY_FILTERED) == 1
    assert coverage.plotting_count(PlottingDisposition.MARKER) == 2


def test_timestamp_slots_must_match_their_eligible_record_partition() -> None:
    missing_record = CoverageLedger(
        timestamps=(TimestampCoverage(AUTHOR_KEY),),
    )
    with pytest.raises(CoverageInvariantError, match="no matching record"):
        missing_record.validate()

    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, 2)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, 2)
    builder.request_slot(AUTHOR_KEY, 1)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.UNAVAILABLE)
    with pytest.raises(CoverageInvariantError, match="eligible records"):
        builder.build()


@pytest.mark.parametrize(
    "coverage",
    [
        RecordCoverage(RECORD_KEY, discovered=1),
        TimestampCoverage(AUTHOR_KEY, requested=1),
        TimestampCoverage(AUTHOR_KEY, requested=1, unavailable=1, included=1),
        TimestampCoverage(AUTHOR_KEY, requested=1, captured=1, included=1),
    ],
)
def test_invariant_mismatches_are_internal_fatal_errors(coverage: RecordCoverage | TimestampCoverage) -> None:
    with pytest.raises(CoverageInvariantError, match="does not reconcile"):
        coverage.validate()


def test_builder_can_emit_unvalidated_snapshot_for_internal_assertion_tests() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY)
    ledger = builder.build(validate=False)
    with pytest.raises(CoverageInvariantError):
        ledger.validate()


def test_ledger_merge_adds_matching_partitions() -> None:
    first = _complete_builder().build()
    merged = first.merge(first)
    assert merged.records_discovered == 6
    assert merged.slots_requested == 4
    assert merged.markers_plotted == 2


def test_ledger_rejects_duplicate_partition_keys_and_negative_counts() -> None:
    item = RecordCoverage(RECORD_KEY)
    with pytest.raises(ValueError, match="duplicate"):
        CoverageLedger((item, item))
    timestamp = TimestampCoverage(AUTHOR_KEY)
    with pytest.raises(ValueError, match="duplicate"):
        CoverageLedger(timestamps=(timestamp, timestamp))
    with pytest.raises(ValueError, match="non-negative"):
        RecordCoverage(RECORD_KEY, discovered=-1)
    with pytest.raises(ValueError, match="non-negative"):
        TimestampCoverage(AUTHOR_KEY, requested=-1)
    with pytest.raises(ValueError, match="non-negative"):
        CoverageLedgerBuilder().discover_record(RECORD_KEY, -1)


def test_timestamp_partition_rejects_cross_source_kind() -> None:
    with pytest.raises(ValueError, match="does not belong"):
        TimestampCoverageKey(Source.FILESYSTEM, "/root", RecordKind.FILESYSTEM_ENTRY, TimestampKind.GIT_AUTHOR)


def test_status_uses_typed_diagnostics_and_success_state() -> None:
    ledger = _complete_builder().build()
    diagnostic = Diagnostic(
        DiagnosticCode.STAT_ERROR,
        DiagnosticStage.EXTRACTION,
        "/root",
        DiagnosticSeverity.ERROR,
        "permission denied",
        Path("private"),
        "record-id",
    )
    assert coverage_status(ledger, (diagnostic,)) is CoverageStatus.PARTIAL
    assert coverage_status(ledger, any_collector_succeeded=False) is CoverageStatus.FAILED

    capability = Capability(
        Source.FILESYSTEM,
        "/root",
        "birth time",
        CapabilityStatus.UNSUPPORTED,
        TimestampKind.FS_CREATED,
        "not exposed by adapter",
    )
    assert capability.status is CapabilityStatus.UNSUPPORTED
