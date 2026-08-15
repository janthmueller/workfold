from __future__ import annotations

import pytest
from workfold.domain.coverage import (
    Capability,
    CapabilityStatus,
    CoverageInvariantError,
    CoverageLedger,
    CoverageLedgerBuilder,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverage,
    TimestampCoverageKey,
    merge_ledgers,
)
from workfold.domain.observations import EntryType, RecordKind, Source, TimestampKind

RECORD_KEY = RecordCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT)
AUTHOR_KEY = TimestampCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT, TimestampKind.GIT_AUTHOR)


def _complete_builder() -> CoverageLedgerBuilder:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, 3)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, 2)
    builder.record_outcome(RECORD_KEY, RecordDisposition.IGNORED, 1)
    builder.examine_slot(AUTHOR_KEY, 2)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.CAPTURED, 1)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.UNAVAILABLE, 1)
    builder.match_scope(AUTHOR_KEY, 1)
    builder.select_observation(AUTHOR_KEY, 1)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.MARKER, 1)
    return builder


def test_ledger_builder_reconciles_extraction_and_selected_results() -> None:
    ledger = _complete_builder().build()
    record = ledger.records[0]
    timestamp = ledger.timestamps[0]

    assert record.count(RecordDisposition.ELIGIBLE) == 2
    assert record.terminal_total == 3
    assert timestamp.extraction_count(ExtractionDisposition.UNAVAILABLE) == 1
    assert timestamp.selected == 1
    assert timestamp.plotting_count(PlottingDisposition.MARKER) == 1
    assert timestamp.extraction_total == timestamp.examined == 2
    assert timestamp.values_read == 1
    assert timestamp.scope_matches == 1
    assert timestamp.plotting_total == timestamp.selected == 1
    assert ledger.records_discovered == 3
    assert ledger.slots_examined == 2
    assert ledger.timestamp_values_read == 1
    assert ledger.timestamp_values_matching_scope == 1
    assert ledger.observations_selected == 1
    assert ledger.markers_plotted == 1
    assert not ledger.has_operational_errors


def test_every_record_disposition_has_a_typed_counter() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, len(RecordDisposition))
    for disposition in RecordDisposition:
        builder.record_outcome(RECORD_KEY, disposition)
    ledger = builder.build()
    assert all(ledger.records[0].count(disposition) == 1 for disposition in RecordDisposition)
    assert ledger.has_operational_errors


def test_extraction_may_examine_more_values_than_the_result_selects() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, len(ExtractionDisposition) + 4)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, len(ExtractionDisposition) + 4)
    builder.examine_slot(AUTHOR_KEY, len(ExtractionDisposition) + 4)
    for disposition in ExtractionDisposition:
        builder.extraction_outcome(
            AUTHOR_KEY,
            disposition,
            5 if disposition is ExtractionDisposition.CAPTURED else 1,
        )
    builder.match_scope(AUTHOR_KEY, 3)
    builder.select_observation(AUTHOR_KEY, 3)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.MARKER, 2)
    builder.plotting_outcome(AUTHOR_KEY, PlottingDisposition.COALESCED_INTO_MARKER)
    coverage = builder.build().timestamps[0]
    assert coverage.extraction_count(ExtractionDisposition.CAPTURED) == 5
    assert all(
        coverage.extraction_count(item) == 1
        for item in ExtractionDisposition
        if item is not ExtractionDisposition.CAPTURED
    )
    assert coverage.selected == 3
    assert coverage.values_read == 5
    assert coverage.plotting_count(PlottingDisposition.MARKER) == 2


def test_matching_value_may_end_in_an_accounted_materialization_error() -> None:
    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY)
    builder.record_outcome(RECORD_KEY, RecordDisposition.RECORD_ERROR)
    builder.examine_slot(AUTHOR_KEY)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.CAPTURED)
    builder.match_scope(AUTHOR_KEY)
    builder.materialization_error(AUTHOR_KEY)

    ledger = builder.build()

    timestamp = ledger.timestamps[0]
    assert timestamp.scope_matches == 1
    assert timestamp.materialization_errors == 1
    assert timestamp.selected == 0
    assert ledger.has_operational_errors


def test_scope_match_cannot_silently_disappear_before_pipeline() -> None:
    coverage = TimestampCoverage(
        AUTHOR_KEY,
        examined=1,
        values_read=1,
        scope_matches=1,
    )

    with pytest.raises(CoverageInvariantError, match="scope matches=1, selected plus materialization errors=0"):
        coverage.validate()


def test_timestamp_partitions_require_records_but_not_record_sized_slot_counts() -> None:
    missing_record = CoverageLedger(
        timestamps=(TimestampCoverage(AUTHOR_KEY),),
    )
    with pytest.raises(CoverageInvariantError, match="no matching record"):
        missing_record.validate()

    builder = CoverageLedgerBuilder()
    builder.discover_record(RECORD_KEY, 2)
    builder.record_outcome(RECORD_KEY, RecordDisposition.ELIGIBLE, 2)
    builder.examine_slot(AUTHOR_KEY, 1)
    builder.extraction_outcome(AUTHOR_KEY, ExtractionDisposition.UNAVAILABLE)
    ledger = builder.build()
    assert ledger.timestamps[0].examined == 1
    assert ledger.records[0].eligible == 2


@pytest.mark.parametrize(
    "coverage",
    [
        RecordCoverage(RECORD_KEY, discovered=1),
        TimestampCoverage(AUTHOR_KEY, examined=1),
        TimestampCoverage(AUTHOR_KEY, examined=1, unavailable=1, scope_matches=1, selected=1),
        TimestampCoverage(AUTHOR_KEY, examined=1, values_read=1, scope_matches=1, selected=1),
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
    merged = merge_ledgers(first, first)
    assert merged.records_discovered == 6
    assert merged.slots_examined == 4
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
        TimestampCoverage(AUTHOR_KEY, examined=-1)
    with pytest.raises(ValueError, match="non-negative"):
        CoverageLedgerBuilder().discover_record(RECORD_KEY, -1)


def test_timestamp_partition_rejects_cross_source_kind() -> None:
    with pytest.raises(ValueError, match="does not belong"):
        TimestampCoverageKey(Source.FILESYSTEM, "/root", RecordKind.FILESYSTEM_ENTRY, TimestampKind.GIT_AUTHOR)


def test_coverage_keys_reject_cross_source_record_kinds() -> None:
    with pytest.raises(ValueError, match="record kind does not belong"):
        RecordCoverageKey(Source.GIT, "/root", RecordKind.FILESYSTEM_ENTRY)


def test_filesystem_timestamp_partitions_require_an_exact_entry_type() -> None:
    with pytest.raises(ValueError, match="requires an entry type"):
        TimestampCoverageKey(
            Source.FILESYSTEM,
            "/root",
            RecordKind.FILESYSTEM_ENTRY,
            TimestampKind.FS_MODIFIED,
        )

    key = TimestampCoverageKey(
        Source.FILESYSTEM,
        "/root",
        RecordKind.FILESYSTEM_ENTRY,
        TimestampKind.FS_MODIFIED,
        EntryType.SYMLINK,
    )
    assert key.entry_type is EntryType.SYMLINK


def test_timestamp_partitions_reject_unsupported_record_timestamp_pairs() -> None:
    with pytest.raises(ValueError, match="does not belong to coverage record kind"):
        TimestampCoverageKey(Source.GIT, "/repo", RecordKind.TAG, TimestampKind.GIT_AUTHOR)

    with pytest.raises(ValueError, match="valid only for filesystem"):
        TimestampCoverageKey(
            Source.GIT,
            "/repo",
            RecordKind.COMMIT,
            TimestampKind.GIT_AUTHOR,
            EntryType.REGULAR_FILE,
        )


def test_capability_retains_platform_availability_context() -> None:
    capability = Capability(
        Source.FILESYSTEM,
        "/root",
        "birth time",
        CapabilityStatus.UNSUPPORTED,
        TimestampKind.FS_CREATED,
        "not exposed by adapter",
    )
    assert capability.status is CapabilityStatus.UNSUPPORTED
