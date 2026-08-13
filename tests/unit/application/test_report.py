from __future__ import annotations

import pytest
from workfold.application.report import ReportRequirements


def test_report_requirements_reject_a_negative_outside_event_limit() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        ReportRequirements(outside_event_limit=-1)
