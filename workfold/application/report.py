"""Renderer-neutral report data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from workfold.application.collection import Collection
from workfold.application.resolution import ResolvedTimeSelection
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import CoverageLedger
from workfold.domain.observations import ClassifiedMarker, RecordOrigin, TimestampKind
from workfold.domain.schedule import Schedule
from workfold.folding import Aggregation


@dataclass(frozen=True, slots=True)
class ReportRequirements:
    """Renderer-selected event detail retained during bounded aggregation."""

    outside_event_limit: int = 0
    retain_git_identities: bool = False

    def __post_init__(self) -> None:
        if self.outside_event_limit < 0:
            raise ValueError("outside_event_limit must not be negative")


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
class OutsideEvent:
    """One outside-hours activity marker represented without terminal layout."""

    local_datetime: datetime
    occurred_at_utc_ns: int
    provenance_id: str
    origin: RecordOrigin
    timestamp_roles: tuple[TimestampKind, ...]


@dataclass(frozen=True, slots=True)
class Report:
    """Complete renderer-neutral output of one successful execution."""

    aggregation: Aggregation
    context: ReportContext
    outside_events: tuple[OutsideEvent, ...]


def build_report(aggregation: Aggregation, context: ReportContext) -> Report:
    """Build a report and project retained outside markers into stable rows."""

    outside_events = tuple(_outside_event(marker) for marker in aggregation.retained_outside_markers)
    return Report(aggregation=aggregation, context=context, outside_events=outside_events)


def _outside_event(classified: ClassifiedMarker) -> OutsideEvent:
    marker = classified.marker
    origin = marker.origin
    return OutsideEvent(
        local_datetime=classified.local_datetime,
        occurred_at_utc_ns=marker.occurred_at_utc_ns,
        provenance_id=marker.marker_id,
        origin=origin,
        timestamp_roles=marker.timestamp_roles,
    )
