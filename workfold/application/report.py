"""Renderer-neutral report data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from workfold.configuration.options import EventListSelection
from workfold.domain.coverage import Capability, CoverageLedger
from workfold.domain.evidence import EvidenceKind, EvidenceSelection
from workfold.domain.observations import ClassifiedMarker, RecordOrigin, Source, TimestampKind
from workfold.domain.schedule import Schedule
from workfold.domain.scope import RefScope
from workfold.folding import Aggregation


@dataclass(frozen=True, slots=True)
class ReportScope:
    """Resolved user-facing scope needed by any report renderer."""

    period_label: str
    timezone_name: str
    schedule: Schedule
    profile_name: str
    evidence: EvidenceSelection
    ref_scope: RefScope
    git_identities: tuple[str, ...]
    include_ignored: bool
    exclusions: tuple[str, ...]

    @property
    def sources(self) -> tuple[Source, ...]:
        return self.evidence.sources

    def includes_source(self, source: Source) -> bool:
        return self.evidence.includes_source(source)


@dataclass(frozen=True, slots=True)
class DiagnosticFacts:
    """Counts needed to describe collection completeness without raw diagnostics."""

    errors: int = 0
    warnings: int = 0
    infos: int = 0
    filesystem_inventory_failures: int = 0
    other_completeness_failures: int = 0

    def __post_init__(self) -> None:
        if min(
            self.errors,
            self.warnings,
            self.infos,
            self.filesystem_inventory_failures,
            self.other_completeness_failures,
        ) < 0:
            raise ValueError("diagnostic report counts must be non-negative")


@dataclass(frozen=True, slots=True)
class GitCommitInputTargetFacts:
    """Commit-scan facts for one file-change derivation target."""

    root: str
    reachable: int
    examined: int
    candidates: int
    hydrated: int
    selected: int
    scope_evaluation_errors: int
    unavailable: int
    parse_failures: int
    operational_errors: int


@dataclass(frozen=True, slots=True)
class GitCommitInputFacts:
    """Aggregate commit inputs used to derive requested file-change evidence."""

    reachable: int
    examined: int
    candidates: int
    hydrated: int
    selected: int
    scope_evaluation_errors: int
    record_errors: int
    targets: tuple[GitCommitInputTargetFacts, ...]


@dataclass(frozen=True, slots=True)
class GitFileChangeTargetFacts:
    """File-change derivation facts for one Git repository."""

    root: str
    commits_requested: int
    successfully_parsed: int
    parse_failures: int
    subprocess_failures: int
    changes_discovered: int


@dataclass(frozen=True, slots=True)
class GitFileChangeFacts:
    """Aggregate Git file-change derivation facts."""

    commits_requested: int
    successfully_parsed: int
    parse_failures: int
    subprocess_failures: int
    changes_discovered: int
    targets: tuple[GitFileChangeTargetFacts, ...]


@dataclass(frozen=True, slots=True)
class GitTagFacts:
    annotated: int
    lightweight: int


@dataclass(frozen=True, slots=True)
class GitReflogFacts:
    available: int
    unavailable: int


@dataclass(frozen=True, slots=True)
class CollectionFacts:
    """Stable report projection of collection outcomes and capabilities."""

    diagnostics: DiagnosticFacts = DiagnosticFacts()
    capabilities: tuple[Capability, ...] = ()
    git_roots: tuple[str, ...] = ()
    filesystem_roots: tuple[str, ...] = ()
    pruned_ignored_subtrees: int = 0
    commit_inputs: GitCommitInputFacts | None = None
    file_changes: GitFileChangeFacts | None = None
    duplicate_commit_ids: int = 0
    duplicate_git_targets: int = 0
    linked_worktree_contexts: int = 0
    tags: GitTagFacts | None = None
    reflogs: GitReflogFacts | None = None
    overlapping_filesystem_roots: int = 0


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Structured scope and accounting facts shared by report renderers."""

    scope: ReportScope
    collection: CollectionFacts
    coverage: CoverageLedger


@dataclass(frozen=True, slots=True)
class ReportRequirements:
    """Renderer-selected event detail retained during bounded aggregation."""

    event_list: EventListSelection | None = None
    event_limit: int = 0
    retain_git_identities: bool = False

    def __post_init__(self) -> None:
        if self.event_limit < 0:
            raise ValueError("event_limit must not be negative")


@dataclass(frozen=True, slots=True)
class ListedEvent:
    """One selected activity marker represented without terminal layout."""

    local_datetime: datetime
    occurred_at_utc_ns: int
    origin: RecordOrigin
    timestamp_roles: tuple[TimestampKind, ...]
    within_schedule: bool


@dataclass(frozen=True, slots=True)
class Report:
    """Complete renderer-neutral output of one successful execution."""

    aggregation: Aggregation
    context: ReportContext
    listed_events: tuple[ListedEvent, ...]
    event_list: EventListSelection | None


def build_report(
    aggregation: Aggregation,
    context: ReportContext,
    event_list: EventListSelection | None = None,
) -> Report:
    """Build a report and project retained detail markers into stable rows."""

    listed_events = tuple(_listed_event(marker, event_list) for marker in aggregation.retained_listed_markers)
    return Report(
        aggregation=aggregation,
        context=context,
        listed_events=listed_events,
        event_list=event_list,
    )


def matches_event_list(classified: ClassifiedMarker, selection: EventListSelection) -> bool:
    """Return whether one classified marker belongs in the requested detail list."""

    if not selection.includes_schedule_state(classified.within_schedule):
        return False
    if not selection.evidence_kinds:
        return True
    selected = frozenset(selection.evidence_kinds)
    origin = classified.marker.origin
    return any(
        EvidenceKind.from_dimensions(origin.record_kind, item.kind, origin.entry_type) in selected
        for item in classified.marker.observations
    )


def _listed_event(classified: ClassifiedMarker, selection: EventListSelection | None) -> ListedEvent:
    marker = classified.marker
    origin = marker.origin
    roles = marker.timestamp_roles
    if selection is not None and selection.evidence_kinds:
        selected = frozenset(selection.evidence_kinds)
        roles = tuple(
            item.kind
            for item in marker.observations
            if EvidenceKind.from_dimensions(origin.record_kind, item.kind, origin.entry_type) in selected
        )
    return ListedEvent(
        local_datetime=classified.local_datetime,
        occurred_at_utc_ns=marker.occurred_at_utc_ns,
        origin=origin,
        timestamp_roles=roles,
        within_schedule=classified.within_schedule,
    )


__all__ = [
    "CollectionFacts",
    "DiagnosticFacts",
    "GitCommitInputFacts",
    "GitCommitInputTargetFacts",
    "GitFileChangeFacts",
    "GitFileChangeTargetFacts",
    "GitReflogFacts",
    "GitTagFacts",
    "ListedEvent",
    "Report",
    "ReportContext",
    "ReportRequirements",
    "ReportScope",
    "build_report",
    "matches_event_list",
]
