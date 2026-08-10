"""Shared, renderer-independent collector result types."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Final

_DEFAULT_DIAGNOSTIC_LIMIT: Final[int] = 256


class DiagnosticSeverity(str, Enum):
    """Machine-readable operational severity independent of rendered text."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CollectorDiagnostic:
    """A structured collector failure or limitation.

    ``message`` is intended for logs and reports, while ``code`` and ``stage``
    are stable fields that orchestration can use without matching prose.
    Repository-controlled values are intentionally kept in separate fields so
    the terminal renderer can sanitize them at its boundary.
    """

    code: str
    stage: str
    target: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    path: str | None = None
    provenance_id: str | None = None
    hint: str | None = None


class DiagnosticBuffer(list[CollectorDiagnostic]):
    """Retain a bounded diagnostic sample plus exact omitted counts."""

    def __init__(self, *, limit: int = _DEFAULT_DIAGNOSTIC_LIMIT) -> None:
        if limit < 1:
            raise ValueError("diagnostic limit must be positive")
        super().__init__()
        self._limit = limit
        self._omitted = 0
        self._omitted_by_severity = {severity: 0 for severity in DiagnosticSeverity}
        self._first_omitted_target: str | None = None

    def append(self, diagnostic: CollectorDiagnostic) -> None:
        if len(self) < self._limit:
            super().append(diagnostic)
            return
        self._omitted += 1
        self._omitted_by_severity[diagnostic.severity] += 1
        if self._first_omitted_target is None:
            self._first_omitted_target = diagnostic.target

    def extend(self, diagnostics: Iterable[CollectorDiagnostic]) -> None:
        for diagnostic in diagnostics:
            self.append(diagnostic)

    @property
    def error_count(self) -> int:
        """Return retained and omitted error diagnostics."""

        return (
            sum(item.severity is DiagnosticSeverity.ERROR for item in self)
            + self._omitted_by_severity[DiagnosticSeverity.ERROR]
        )

    def snapshot(self) -> tuple[CollectorDiagnostic, ...]:
        """Return retained diagnostics and one exact truncation summary."""

        if not self._omitted:
            return tuple(self)
        errors = self._omitted_by_severity[DiagnosticSeverity.ERROR]
        warnings = self._omitted_by_severity[DiagnosticSeverity.WARNING]
        infos = self._omitted_by_severity[DiagnosticSeverity.INFO]
        severity = (
            DiagnosticSeverity.ERROR if errors else DiagnosticSeverity.WARNING if warnings else DiagnosticSeverity.INFO
        )
        summary = CollectorDiagnostic(
            code="diagnostics_truncated",
            stage="diagnostic_collection",
            target=self._first_omitted_target or "multiple targets",
            severity=severity,
            message=(
                f"{self._omitted:,} additional diagnostic(s) omitted "
                f"(errors={errors:,}, warnings={warnings:,}, info={infos:,})"
            ),
            hint="Use narrower paths or repair the first reported failures before retrying.",
        )
        return (*self, summary)


__all__ = ["CollectorDiagnostic", "DiagnosticBuffer", "DiagnosticSeverity"]
