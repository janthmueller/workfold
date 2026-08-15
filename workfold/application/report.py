"""Renderer-neutral report data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from workfold.application.collection import Collection
from workfold.application.resolution import ResolvedTimeSelection
from workfold.configuration.options import EventListSelection, RunOptions
from workfold.domain.coverage import CoverageLedger
from workfold.domain.evidence import EvidenceKind
from workfold.domain.observations import ClassifiedMarker, RecordOrigin, TimestampKind
from workfold.domain.schedule import Schedule
from workfold.folding import Aggregation


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
class ReportContext:
    """Structured scope and accounting facts shared by report renderers."""

    options: RunOptions
    collection: Collection
    time_selection: ResolvedTimeSelection
    timezone: ZoneInfo
    schedule: Schedule
    coverage: CoverageLedger


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
    "ListedEvent",
    "Report",
    "ReportContext",
    "ReportRequirements",
    "build_report",
    "matches_event_list",
]
