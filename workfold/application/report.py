"""Renderer-neutral report data transfer objects."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from workfold.configuration.options import EventListSelection
from workfold.domain.coverage import Capability, CapabilityKind, CapabilityStatus, CoverageLedger
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
    profile_name: str | None
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
    filesystem_inventory_errors: int = 0
    other_completeness_failures: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.errors,
                self.warnings,
                self.infos,
                self.filesystem_inventory_failures,
                self.filesystem_inventory_errors,
                self.other_completeness_failures,
            )
            < 0
        ):
            raise ValueError("diagnostic report counts must be non-negative")
        if self.filesystem_inventory_errors > self.errors:
            raise ValueError("filesystem inventory errors cannot exceed all diagnostic errors")


@dataclass(frozen=True, slots=True)
class CapabilityLimitation:
    """One unsupported capability qualified across selected targets."""

    kind: CapabilityKind
    name: str
    affected_targets: int
    total_targets: int
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 < self.affected_targets <= self.total_targets:
            raise ValueError("capability limitation target counts must be positive and ordered")


@dataclass(frozen=True, slots=True)
class CompletenessAssessment:
    """Application-owned verdict and qualifications for requested coverage."""

    partial: bool
    filesystem_inventory_failures: int = 0
    collection_errors: int = 0
    collection_warnings: int = 0
    ledger_has_operational_errors: bool = False
    git_identity_scope_active: bool = False
    explicit_exclusions_active: bool = False
    pruned_ignored_subtrees: int = 0
    capability_limitations: tuple[CapabilityLimitation, ...] = ()
    unavailable_timestamps: tuple[tuple[TimestampKind, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            min(
                self.filesystem_inventory_failures,
                self.collection_errors,
                self.collection_warnings,
                self.pruned_ignored_subtrees,
            )
            < 0
        ):
            raise ValueError("completeness assessment counts must be non-negative")
        if any(count <= 0 for _kind, count in self.unavailable_timestamps):
            raise ValueError("unavailable timestamp counts must be positive")

    @property
    def is_partial(self) -> bool:
        """Return the single authoritative completeness verdict."""

        return self.partial


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

    @property
    def completeness(self) -> CompletenessAssessment:
        """Assess completeness once in the renderer-neutral application model."""

        return assess_completeness(self.collection, self.coverage, self.scope)


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


def assess_completeness(
    collection: CollectionFacts,
    ledger: CoverageLedger,
    scope: ReportScope,
) -> CompletenessAssessment:
    """Combine diagnostics, ledger failures, and qualifications consistently."""

    diagnostics = collection.diagnostics
    represented_inventory_errors = min(
        diagnostics.filesystem_inventory_errors,
        diagnostics.filesystem_inventory_failures,
    )
    collection_errors = diagnostics.errors - represented_inventory_errors
    ledger_incomplete = ledger.has_operational_errors
    partial = bool(
        diagnostics.errors
        or diagnostics.filesystem_inventory_failures
        or diagnostics.other_completeness_failures
        or ledger_incomplete
    )

    capabilities_by_kind: dict[CapabilityKind, list[Capability]] = {}
    for capability in collection.capabilities:
        capabilities_by_kind.setdefault(capability.kind, []).append(capability)
    limitations: list[CapabilityLimitation] = []
    for kind, capabilities in capabilities_by_kind.items():
        unsupported = tuple(item for item in capabilities if item.status is CapabilityStatus.UNSUPPORTED)
        if not unsupported:
            continue
        targets = {item.target for item in capabilities}
        affected = {item.target for item in unsupported}
        limitations.append(
            CapabilityLimitation(
                kind=kind,
                name=unsupported[0].name,
                affected_targets=len(affected),
                total_targets=len(targets),
                notes=tuple(sorted({item.note for item in unsupported if item.note})),
            )
        )

    unavailable = Counter[TimestampKind]()
    for item in ledger.timestamps:
        unavailable[item.key.timestamp_kind] += item.unavailable
    return CompletenessAssessment(
        partial=partial,
        filesystem_inventory_failures=diagnostics.filesystem_inventory_failures,
        collection_errors=collection_errors,
        collection_warnings=diagnostics.other_completeness_failures,
        ledger_has_operational_errors=ledger_incomplete,
        git_identity_scope_active=bool(scope.git_identities),
        explicit_exclusions_active=bool(scope.exclusions),
        pruned_ignored_subtrees=collection.pruned_ignored_subtrees,
        capability_limitations=tuple(sorted(limitations, key=lambda item: item.kind.value)),
        unavailable_timestamps=tuple(sorted((kind, count) for kind, count in unavailable.items() if count)),
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
    "CapabilityLimitation",
    "CompletenessAssessment",
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
    "assess_completeness",
    "matches_event_list",
]
