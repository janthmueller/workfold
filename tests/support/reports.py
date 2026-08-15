from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from workfold.application.collection import Collection
from workfold.application.report import Report, build_report, matches_event_list
from workfold.application.report_context import build_report_context
from workfold.application.resolution import ResolvedTimeSelection
from workfold.cli import parse_options
from workfold.configuration import ClusterAnchor, EventListSelection, ListSchedule
from workfold.domain.coverage import CoverageLedger
from workfold.domain.observations import (
    ActivityMarker,
    ClassifiedMarker,
    EntryType,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    Weekday,
)
from workfold.domain.schedule import parse_schedule
from workfold.domain.time import all_time_range
from workfold.folding import aggregate_markers


def classified_marker(
    identifier: str,
    hour: int,
    minute: int,
    *,
    second: int = 0,
    source: Source,
    within_schedule: bool,
    description: str | None = None,
    root: Path = Path("/work/repository"),
    fractional_nanoseconds: int = 0,
    actor_name: str = "Fixture",
    actor_email: str = "fixture@example.test",
    day: int = 3,
) -> ClassifiedMarker:
    local_datetime = datetime(
        2026,
        8,
        day,
        hour,
        minute,
        second,
        microsecond=fractional_nanoseconds // 1_000,
        tzinfo=timezone.utc,
    )
    is_git = source is Source.GIT
    origin = RecordOrigin(
        record_id=f"record-{identifier}",
        source=source,
        record_kind=RecordKind.COMMIT if is_git else RecordKind.FILESYSTEM_ENTRY,
        repository_or_root=root,
        path=None if is_git else Path(f"files/{identifier}"),
        commit_id=identifier if is_git else None,
        entry_type=None if is_git else EntryType.REGULAR_FILE,
        description=description,
    )
    kind = TimestampKind.GIT_AUTHOR if is_git else TimestampKind.FS_MODIFIED
    instant_ns = int(local_datetime.replace(microsecond=0).timestamp()) * 1_000_000_000 + fractional_nanoseconds
    marker = ActivityMarker.create(
        (
            TimestampObservation.create(
                origin,
                kind,
                instant_ns,
                str(instant_ns),
                original_offset_minutes=0 if is_git else None,
                actor_name=actor_name if is_git else None,
                actor_email=actor_email if is_git else None,
            ),
        )
    )
    return ClassifiedMarker(marker, local_datetime, within_schedule)


def report(
    *markers: ClassifiedMarker,
    event_limit: int = 50,
    event_list: EventListSelection | None = None,
    cluster_window: timedelta = timedelta(hours=1),
    cluster_anchor: ClusterAnchor = ClusterAnchor.EVENT,
    schedule_bounds: tuple[int, int] | None = None,
    display_range: tuple[int, int] | None = None,
    retain_git_identities: bool = False,
    hide_days: tuple[Weekday, ...] = (),
    hide_empty_days: tuple[Weekday, ...] = (),
) -> Report:
    selected_list = event_list or EventListSelection(schedule=ListSchedule.OUTSIDE)
    aggregation = aggregate_markers(
        markers,
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        schedule_bounds=schedule_bounds,
        display_range=display_range,
        listed_marker_limit=event_limit,
        listed_marker_predicate=lambda marker: matches_event_list(marker, selected_list),
        retain_git_identities=retain_git_identities,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
    )
    options = parse_options(["--mode", "both", "--time", "2026-W32", "--timezone", "UTC"])
    context = build_report_context(
        options=options,
        collection=Collection((), (), True),
        time_selection=ResolvedTimeSelection(all_time_range(), "2026-W32"),
        timezone=ZoneInfo("UTC"),
        schedule=parse_schedule(options.hours),
        coverage=CoverageLedger(),
    )
    return build_report(aggregation, context, selected_list)


def coalesced_git_marker(identifier: str, hour: int, *, within_schedule: bool) -> ClassifiedMarker:
    local_datetime = datetime(2026, 8, 3, hour, tzinfo=timezone.utc)
    instant_ns = int(local_datetime.timestamp()) * 1_000_000_000
    origin = RecordOrigin(
        record_id=f"record-{identifier}",
        source=Source.GIT,
        record_kind=RecordKind.COMMIT,
        repository_or_root=Path("/work/repository"),
        commit_id=identifier,
    )
    author = TimestampObservation.create(
        origin,
        TimestampKind.GIT_AUTHOR,
        instant_ns,
        str(instant_ns),
        original_offset_minutes=0,
        actor_name="Ada Author",
        actor_email="ada@example.test",
    )
    committer = TimestampObservation.create(
        origin,
        TimestampKind.GIT_COMMITTER,
        instant_ns,
        str(instant_ns),
        original_offset_minutes=0,
        actor_name="Bob Committer",
        actor_email="bob@example.test",
    )
    return ClassifiedMarker(ActivityMarker.create((author, committer)), local_datetime, within_schedule)


__all__ = ["classified_marker", "coalesced_git_marker", "report"]
