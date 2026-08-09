"""Renderer-neutral report data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from workfold.aggregation import Aggregation
from workfold.models import ClassifiedMarker, RecordKind, RecordOrigin, Source

COMPLETE_COVERAGE_STATUS = "complete for all discoverable timestamps in the requested scope"


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Human-facing scope facts shared by every future renderer."""

    source_label: str
    range_label: str
    timezone_label: str
    schedule_label: str
    coverage_status: str
    profile_label: str = "standard"
    extent_label: str | None = None
    enabled_sources: tuple[Source, ...] = ()
    enabled_record_kinds: tuple[RecordKind, ...] = ()
    identity_label: str | None = None
    ignore_label: str | None = None
    exclusions: tuple[str, ...] = ()
    coverage_details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OutsideEvent:
    """One outside-hours activity marker represented without terminal layout."""

    local_datetime: datetime
    occurred_at_utc_ns: int
    provenance_id: str
    source: Source
    timestamp_roles: tuple[str, ...]
    repository_or_root: Path
    commit_id: str | None
    ref_name: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class Report:
    """Complete renderer input for the terminal MVP."""

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
    description = _event_description(origin)
    return OutsideEvent(
        local_datetime=classified.local_datetime,
        occurred_at_utc_ns=marker.occurred_at_utc_ns,
        provenance_id=marker.marker_id,
        source=origin.source,
        timestamp_roles=tuple(role.value for role in marker.timestamp_roles),
        repository_or_root=origin.repository_or_root,
        commit_id=origin.commit_id,
        ref_name=origin.ref_name,
        description=description,
    )


def _event_description(origin: RecordOrigin) -> str | None:
    """Build source-aware detail without hiding file-change provenance."""

    if origin.record_kind is RecordKind.GIT_FILE_CHANGE:
        change = origin.change_kind.value if origin.change_kind is not None else "changed"
        if origin.old_path is not None and origin.path is not None:
            path_detail = f"{origin.old_path} -> {origin.path}"
        elif origin.path is not None:
            path_detail = str(origin.path)
        else:
            path_detail = "unknown path"
        file_detail = f"{change}: {path_detail}"
        return f"{file_detail} | {origin.description}" if origin.description else file_detail
    if origin.description is not None:
        return origin.description
    if origin.old_path is not None and origin.path is not None:
        return f"{origin.old_path} -> {origin.path}"
    if origin.path is not None:
        return str(origin.path)
    return None
