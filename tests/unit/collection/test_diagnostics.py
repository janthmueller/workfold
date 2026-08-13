from __future__ import annotations

import pytest
from workfold.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity


def _diagnostic(
    index: int,
    severity: DiagnosticSeverity,
    *,
    affects_completeness: bool = False,
) -> CollectorDiagnostic:
    return CollectorDiagnostic(
        code=f"failure_{index}",
        stage="fixture",
        target=f"target-{index}",
        message=f"failure {index}",
        severity=severity,
        affects_completeness=affects_completeness,
    )


def test_diagnostic_buffer_retains_a_bounded_sample_and_exact_omitted_counts() -> None:
    diagnostics = DiagnosticBuffer(limit=2)
    diagnostics.extend(
        (
            _diagnostic(1, DiagnosticSeverity.WARNING),
            _diagnostic(2, DiagnosticSeverity.ERROR),
            _diagnostic(3, DiagnosticSeverity.INFO),
            _diagnostic(4, DiagnosticSeverity.ERROR),
        )
    )

    snapshot = diagnostics.snapshot()

    assert len(diagnostics) == 2
    assert diagnostics.error_count == 2
    assert [item.code for item in snapshot] == ["failure_1", "failure_2", "diagnostics_truncated"]
    assert snapshot[-1].severity is DiagnosticSeverity.ERROR
    assert snapshot[-1].target == "target-3"
    assert "errors=1" in snapshot[-1].message
    assert "info=1" in snapshot[-1].message
    assert snapshot[-1].occurrence_count(DiagnosticSeverity.ERROR) == 1
    assert snapshot[-1].occurrence_count(DiagnosticSeverity.INFO) == 1


def test_diagnostic_buffer_validates_its_limit_and_omits_no_summary_when_complete() -> None:
    with pytest.raises(ValueError, match="positive"):
        DiagnosticBuffer(limit=0)

    diagnostics = DiagnosticBuffer(limit=1)
    diagnostic = _diagnostic(1, DiagnosticSeverity.WARNING)
    diagnostics.append(diagnostic)

    assert diagnostics.snapshot() == (diagnostic,)
    assert diagnostics.error_count == 0


def test_diagnostic_buffer_preserves_completeness_impact_when_sample_is_truncated() -> None:
    diagnostics = DiagnosticBuffer(limit=1)
    diagnostics.append(_diagnostic(1, DiagnosticSeverity.INFO))
    diagnostics.append(_diagnostic(2, DiagnosticSeverity.WARNING, affects_completeness=True))

    summary = diagnostics.snapshot()[-1]

    assert summary.code == "diagnostics_truncated"
    assert summary.severity is DiagnosticSeverity.WARNING
    assert summary.affects_completeness
    assert summary.completeness_failure_count == 1


def test_diagnostic_buffer_preserves_nested_truncation_summary_counts() -> None:
    inner = DiagnosticBuffer(limit=1)
    inner.append(_diagnostic(1, DiagnosticSeverity.ERROR))
    inner.append(_diagnostic(2, DiagnosticSeverity.WARNING, affects_completeness=True))
    inner.append(_diagnostic(3, DiagnosticSeverity.INFO))

    outer = DiagnosticBuffer(limit=1)
    outer.append(_diagnostic(4, DiagnosticSeverity.INFO))
    outer.extend(inner.snapshot())
    summary = outer.snapshot()[-1]

    assert summary.occurrence_count(DiagnosticSeverity.ERROR) == 1
    assert summary.occurrence_count(DiagnosticSeverity.WARNING) == 1
    assert summary.occurrence_count(DiagnosticSeverity.INFO) == 1
    assert summary.completeness_failure_count == 1
    assert summary.message.startswith("3 additional diagnostic(s) omitted")
