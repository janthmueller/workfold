"""Terminal coverage presentation tests."""

from workfold.application.report import CollectionFacts, DiagnosticFacts, assess_completeness
from workfold.domain.coverage import (
    Capability,
    CapabilityKind,
    CapabilityStatus,
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
    label = coverage_status_label(assess_completeness(context.collection, builder.build(), context.scope))

    assert label.endswith("1 Git author timestamp unavailable on source record")


def test_inventory_error_without_completeness_annotation_remains_partial() -> None:
    context = report().context
    assessment = assess_completeness(
        CollectionFacts(
            diagnostics=DiagnosticFacts(
                errors=1,
                filesystem_inventory_errors=1,
            )
        ),
        context.coverage,
        context.scope,
    )

    assert assessment.is_partial
    assert coverage_status_label(assessment) == "partial · 1 collection error"


def test_unsupported_capability_is_qualified_across_selected_targets() -> None:
    context = report().context
    collection = CollectionFacts(
        capabilities=(
            Capability(
                Source.FILESYSTEM,
                "/one",
                CapabilityKind.FS_CREATED_TIME,
                "filesystem creation/birth time",
                CapabilityStatus.UNSUPPORTED,
                TimestampKind.FS_CREATED,
                "platform API unavailable",
            ),
            Capability(
                Source.FILESYSTEM,
                "/two",
                CapabilityKind.FS_CREATED_TIME,
                "filesystem creation/birth time",
                CapabilityStatus.SUPPORTED,
                TimestampKind.FS_CREATED,
            ),
        )
    )

    label = coverage_status_label(assess_completeness(collection, context.coverage, context.scope))

    assert label.endswith("filesystem creation/birth time unavailable on 1 of 2 targets: platform API unavailable")
