from __future__ import annotations

import pytest
from wuf.application.report import ReportRequirements


def test_report_requirements_reject_a_negative_event_limit() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        ReportRequirements(event_limit=-1)
