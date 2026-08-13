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
class DiagnosticOccurrences:
    """Exact severity/completeness counts represented by one diagnostic."""

    errors: int = 0
    warnings: int = 0
    infos: int = 0
    completeness_failures: int = 0

    def __post_init__(self) -> None:
        values = (self.errors, self.warnings, self.infos, self.completeness_failures)
        if any(value < 0 for value in values):
            raise ValueError("diagnostic occurrence counts must be non-negative")
        if self.completeness_failures > self.total:
            raise ValueError("completeness failures cannot exceed diagnostic occurrences")

    @property
    def total(self) -> int:
        return self.errors + self.warnings + self.infos

    def count(self, severity: DiagnosticSeverity) -> int:
        return {
            DiagnosticSeverity.ERROR: self.errors,
            DiagnosticSeverity.WARNING: self.warnings,
            DiagnosticSeverity.INFO: self.infos,
        }[severity]


@dataclass(frozen=True, slots=True)
class CollectorDiagnostic:
    """A structured collector failure or limitation.

    ``message`` is intended for logs and reports, while ``code`` and ``stage``
    are stable fields that orchestration can use without matching prose.
    Repository-controlled values are intentionally kept in separate fields so
    the terminal renderer can sanitize them at its boundary.

    ``affects_completeness`` distinguishes a usable but incomplete collection
    from an informational warning. Application policy may then decide whether
    that condition should make the command fail.
    """

    code: str
    stage: str
    target: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    path: str | None = None
    provenance_id: str | None = None
    hint: str | None = None
    affects_completeness: bool = False
    represented_occurrences: DiagnosticOccurrences | None = None

    def __post_init__(self) -> None:
        if self.represented_occurrences is not None and self.represented_occurrences.total < 1:
            raise ValueError("a diagnostic must represent at least one occurrence")

    def occurrence_count(self, severity: DiagnosticSeverity) -> int:
        """Return exact occurrences of one severity represented by this row."""

        if self.represented_occurrences is not None:
            return self.represented_occurrences.count(severity)
        return int(self.severity is severity)

    @property
    def completeness_failure_count(self) -> int:
        """Return exact completeness-affecting occurrences represented here."""

        if self.represented_occurrences is not None:
            return self.represented_occurrences.completeness_failures
        return int(self.affects_completeness)


class DiagnosticBuffer(list[CollectorDiagnostic]):
    """Retain a bounded diagnostic sample plus exact omitted counts."""

    def __init__(self, *, limit: int = _DEFAULT_DIAGNOSTIC_LIMIT) -> None:
        if limit < 1:
            raise ValueError("diagnostic limit must be positive")
        super().__init__()
        self._limit = limit
        self._omitted = 0
        self._omitted_by_severity = {severity: 0 for severity in DiagnosticSeverity}
        self._omitted_completeness_failures = 0
        self._first_omitted_target: str | None = None

    def append(self, diagnostic: CollectorDiagnostic) -> None:
        if len(self) < self._limit:
            super().append(diagnostic)
            return
        represented = diagnostic.represented_occurrences
        if represented is None:
            self._omitted += 1
            self._omitted_by_severity[diagnostic.severity] += 1
        else:
            self._omitted += represented.total
            for severity in DiagnosticSeverity:
                self._omitted_by_severity[severity] += represented.count(severity)
        self._omitted_completeness_failures += diagnostic.completeness_failure_count
        if self._first_omitted_target is None:
            self._first_omitted_target = diagnostic.target

    def extend(self, diagnostics: Iterable[CollectorDiagnostic]) -> None:
        for diagnostic in diagnostics:
            self.append(diagnostic)

    @property
    def error_count(self) -> int:
        """Return retained and omitted error diagnostics."""

        return (
            sum(item.occurrence_count(DiagnosticSeverity.ERROR) for item in self)
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
            affects_completeness=bool(self._omitted_completeness_failures),
            represented_occurrences=DiagnosticOccurrences(
                errors=errors,
                warnings=warnings,
                infos=infos,
                completeness_failures=self._omitted_completeness_failures,
            ),
        )
        return (*self, summary)


def diagnostics_are_partial(diagnostics: Iterable[CollectorDiagnostic]) -> bool:
    """Return whether diagnostics report missing requested collection scope."""

    return any(
        item.occurrence_count(DiagnosticSeverity.ERROR) or item.completeness_failure_count for item in diagnostics
    )


__all__ = [
    "CollectorDiagnostic",
    "DiagnosticBuffer",
    "DiagnosticOccurrences",
    "DiagnosticSeverity",
    "diagnostics_are_partial",
]
