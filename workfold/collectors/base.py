"""Shared, renderer-independent collector result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


__all__ = ["CollectorDiagnostic", "DiagnosticSeverity"]
