"""Resolve user-facing time and timestamp selections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from workfold.configuration.local_timezone import resolve_local_timezone
from workfold.configuration.options import FilesystemTime, GitDateMode, RunOptions, UsageError
from workfold.domain.observations import TimestampKind
from workfold.domain.schedule import Schedule, parse_schedule
from workfold.domain.time import (
    InstantRangeUnion,
    TimeRangeError,
    all_time_range,
    calendar_date_range,
    current_week_range,
    iso_week_union,
    resolve_timezone,
    rolling_duration_range,
)


@dataclass(frozen=True, slots=True)
class ResolvedTimeSelection:
    """Exact selected instants plus their canonical user-facing selector."""

    ranges: InstantRangeUnion
    label: str


def resolve_timezone_selection(options: RunOptions, environ: Mapping[str, str]) -> ZoneInfo:
    """Resolve the configured or operating-system-local timezone."""

    try:
        if options.timezone_name is not None:
            return resolve_timezone(options.timezone_name)
        return resolve_local_timezone(environ=environ)
    except TimeRangeError as error:
        raise UsageError(str(error)) from error


def resolve_date_range(
    options: RunOptions,
    timezone_value: ZoneInfo,
    now: datetime,
) -> ResolvedTimeSelection:
    """Build the normalized range union and its canonical selector label."""

    try:
        if options.weeks:
            return ResolvedTimeSelection(iso_week_union(options.weeks, timezone_value), ", ".join(options.weeks))
        if options.from_date is not None or options.to_date is not None:
            label = _calendar_range_label(options.from_date, options.to_date)
            return ResolvedTimeSelection(
                calendar_date_range(options.from_date, options.to_date, timezone_value),
                label,
            )
        if options.rolling_duration is not None:
            rolling = options.rolling_duration
            return ResolvedTimeSelection(rolling_duration_range(now, rolling.duration), f"last {rolling.label}")
        if options.all_dates:
            return ResolvedTimeSelection(all_time_range(), "all available dates")
        local_now = now.astimezone(timezone_value)
        iso = local_now.isocalendar()
        return ResolvedTimeSelection(
            current_week_range(now, timezone_value),
            f"{iso.year:04d}-W{iso.week:02d}",
        )
    except (TimeRangeError, ValueError) as error:
        raise UsageError(str(error)) from error


def resolve_schedule(options: RunOptions) -> Schedule:
    """Parse the configured working-hours expression."""

    try:
        return parse_schedule(options.hours)
    except ValueError as error:
        raise UsageError(str(error)) from error


def validate_without_collection(options: RunOptions, *, environ: Mapping[str, str]) -> None:
    """Validate environment-dependent options without touching evidence sources."""

    timezone_value = resolve_timezone_selection(options, environ)
    resolve_schedule(options)
    resolve_date_range(options, timezone_value, datetime.now(timezone.utc))


def git_timestamp_kinds(mode: GitDateMode) -> tuple[TimestampKind, ...]:
    """Map the Git date mode to normalized timestamp kinds."""

    if mode is GitDateMode.AUTHOR:
        return (TimestampKind.GIT_AUTHOR,)
    if mode is GitDateMode.COMMITTER:
        return (TimestampKind.GIT_COMMITTER,)
    return (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER)


def filesystem_timestamp_kinds(values: tuple[FilesystemTime, ...]) -> tuple[TimestampKind, ...]:
    """Map filesystem time selections to normalized timestamp kinds."""

    mapping = {
        FilesystemTime.CREATED: TimestampKind.FS_CREATED,
        FilesystemTime.MODIFIED: TimestampKind.FS_MODIFIED,
        FilesystemTime.CHANGED: TimestampKind.FS_METADATA_CHANGED,
        FilesystemTime.ACCESSED: TimestampKind.FS_ACCESSED,
    }
    return tuple(mapping[value] for value in values)


def _calendar_range_label(start: date | None, end: date | None) -> str:
    if start is None:
        return f"through {end}"
    if end is None:
        return f"from {start}"
    return f"{start}..{end}"
