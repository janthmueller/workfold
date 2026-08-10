from __future__ import annotations

import pytest
from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity


def _diagnostic(index: int, severity: DiagnosticSeverity) -> CollectorDiagnostic:
    return CollectorDiagnostic(
        code=f"failure_{index}",
        stage="fixture",
        target=f"target-{index}",
        message=f"failure {index}",
        severity=severity,
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


def test_diagnostic_buffer_validates_its_limit_and_omits_no_summary_when_complete() -> None:
    with pytest.raises(ValueError, match="positive"):
        DiagnosticBuffer(limit=0)

    diagnostics = DiagnosticBuffer(limit=1)
    diagnostic = _diagnostic(1, DiagnosticSeverity.WARNING)
    diagnostics.append(diagnostic)

    assert diagnostics.snapshot() == (diagnostic,)
    assert diagnostics.error_count == 0
