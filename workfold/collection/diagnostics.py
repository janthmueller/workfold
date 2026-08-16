"""Shared, renderer-independent collector result types."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, overload

_DEFAULT_DIAGNOSTIC_LIMIT: Final[int] = 256


class DiagnosticCategory(str, Enum):
    """Stable policy category independent of source-specific diagnostic codes."""

    INVOCATION = "invocation"
    COLLECTION = "collection"


class DiagnosticKind(str, Enum):
    """Typed semantic identity for diagnostics with dedicated handling."""

    GENERAL = "general"
    FILESYSTEM_INVENTORY = "filesystem_inventory"


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
    category: DiagnosticCategory = DiagnosticCategory.COLLECTION
    kind: DiagnosticKind = DiagnosticKind.GENERAL

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


class DiagnosticSink(Protocol):
    """Minimal append-only diagnostic port accepted by collectors."""

    def append(self, diagnostic: CollectorDiagnostic, /) -> None:
        """Record one structured diagnostic."""

        ...


@dataclass(slots=True)
class _OmittedDiagnosticPartition:
    """Mutable counts for one policy-category and semantic-kind partition."""

    first_target: str
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    completeness_failures: int = 0

    @property
    def total(self) -> int:
        return self.errors + self.warnings + self.infos

    def add(self, diagnostic: CollectorDiagnostic) -> None:
        represented = diagnostic.represented_occurrences
        if represented is None:
            if diagnostic.severity is DiagnosticSeverity.ERROR:
                self.errors += 1
            elif diagnostic.severity is DiagnosticSeverity.WARNING:
                self.warnings += 1
            else:
                self.infos += 1
        else:
            self.errors += represented.errors
            self.warnings += represented.warnings
            self.infos += represented.infos
        self.completeness_failures += diagnostic.completeness_failure_count


class DiagnosticBuffer(Sequence[CollectorDiagnostic]):
    """Retain a bounded sample plus exact typed summaries for omissions."""

    def __init__(self, *, limit: int = _DEFAULT_DIAGNOSTIC_LIMIT) -> None:
        if limit < 1:
            raise ValueError("diagnostic limit must be positive")
        self._items: list[CollectorDiagnostic] = []
        self._limit = limit
        self._omitted: dict[
            tuple[DiagnosticCategory, DiagnosticKind],
            _OmittedDiagnosticPartition,
        ] = {}

    def append(self, diagnostic: CollectorDiagnostic) -> None:
        if len(self._items) < self._limit:
            self._items.append(diagnostic)
            return
        key = (diagnostic.category, diagnostic.kind)
        partition = self._omitted.setdefault(key, _OmittedDiagnosticPartition(diagnostic.target))
        partition.add(diagnostic)

    def extend(self, diagnostics: Iterable[CollectorDiagnostic]) -> None:
        for diagnostic in diagnostics:
            self.append(diagnostic)

    def __len__(self) -> int:
        return len(self._items)

    @overload
    def __getitem__(self, index: int) -> CollectorDiagnostic: ...

    @overload
    def __getitem__(self, index: slice) -> list[CollectorDiagnostic]: ...

    def __getitem__(self, index: int | slice) -> CollectorDiagnostic | list[CollectorDiagnostic]:
        return self._items[index]

    def __iter__(self) -> Iterator[CollectorDiagnostic]:
        return iter(self._items)

    @property
    def error_count(self) -> int:
        """Return retained and omitted error diagnostics."""

        return sum(item.occurrence_count(DiagnosticSeverity.ERROR) for item in self._items) + sum(
            partition.errors for partition in self._omitted.values()
        )

    def snapshot(self) -> tuple[CollectorDiagnostic, ...]:
        """Return retained diagnostics and exact typed truncation summaries."""

        if not self._omitted:
            return tuple(self._items)
        summaries = tuple(
            _truncation_summary(category, kind, partition) for (category, kind), partition in self._omitted.items()
        )
        return (*self._items, *summaries)


def _truncation_summary(
    category: DiagnosticCategory,
    kind: DiagnosticKind,
    partition: _OmittedDiagnosticPartition,
) -> CollectorDiagnostic:
    severity = (
        DiagnosticSeverity.ERROR
        if partition.errors
        else DiagnosticSeverity.WARNING
        if partition.warnings
        else DiagnosticSeverity.INFO
    )
    return CollectorDiagnostic(
        code="diagnostics_truncated",
        stage="diagnostic_collection",
        target=partition.first_target,
        severity=severity,
        message=(
            f"{partition.total:,} additional diagnostic(s) omitted "
            f"(errors={partition.errors:,}, warnings={partition.warnings:,}, info={partition.infos:,})"
        ),
        hint="Use narrower paths or repair the first reported failures before retrying.",
        affects_completeness=bool(partition.completeness_failures),
        represented_occurrences=DiagnosticOccurrences(
            errors=partition.errors,
            warnings=partition.warnings,
            infos=partition.infos,
            completeness_failures=partition.completeness_failures,
        ),
        category=category,
        kind=kind,
    )


def diagnostics_are_partial(diagnostics: Iterable[CollectorDiagnostic]) -> bool:
    """Return whether diagnostics report missing requested collection scope."""

    return any(
        item.occurrence_count(DiagnosticSeverity.ERROR) or item.completeness_failure_count for item in diagnostics
    )


__all__ = [
    "CollectorDiagnostic",
    "DiagnosticCategory",
    "DiagnosticKind",
    "DiagnosticBuffer",
    "DiagnosticOccurrences",
    "DiagnosticSeverity",
    "DiagnosticSink",
    "diagnostics_are_partial",
]
