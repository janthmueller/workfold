"""Terminal coverage presentation tests."""

from workfold.domain.coverage import (
    CoverageLedgerBuilder,
    ExtractionDisposition,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverageKey,
)
from workfold.domain.observations import RecordKind, Source, TimestampKind
from workfold.reporting.terminal.coverage_status import coverage_status_label

from support.reports import report


def test_default_coverage_status_exposes_accounted_unavailable_timestamp_slots() -> None:
    record_key = RecordCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT)
    timestamp_key = TimestampCoverageKey(Source.GIT, "/repo", RecordKind.COMMIT, TimestampKind.GIT_AUTHOR)
    builder = CoverageLedgerBuilder()
    builder.discover_record(record_key)
    builder.record_outcome(record_key, RecordDisposition.ELIGIBLE)
    builder.examine_slot(timestamp_key)
    builder.extraction_outcome(timestamp_key, ExtractionDisposition.UNAVAILABLE)

    context = report().context
    label = coverage_status_label(context.collection, builder.build(), context.scope)

    assert label.endswith("1 Git author timestamp unavailable on source record")
