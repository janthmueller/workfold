"""Resolve user-facing time and timestamp selections."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

from workfold.config import FilesystemTime, GitDateMode, RawOptions, UsageError
from workfold.models import TimestampKind
from workfold.schedule import Schedule, parse_schedule
from workfold.time_ranges import (
    InstantRangeUnion,
    TimeRangeError,
    all_time_range,
    calendar_date_range,
    current_week_range,
    iso_week_union,
    resolve_local_timezone,
    resolve_timezone,
)


def resolve_timezone_selection(options: RawOptions, environ: Mapping[str, str]) -> ZoneInfo:
    """Resolve the configured or operating-system-local timezone."""

    try:
        if options.timezone_name is not None:
            return resolve_timezone(options.timezone_name)
        return resolve_local_timezone(environ=environ)
    except TimeRangeError as error:
        raise UsageError(str(error)) from error


def resolve_date_range(
    options: RawOptions,
    timezone_value: ZoneInfo,
    now: datetime,
) -> tuple[InstantRangeUnion, str]:
    """Build the normalized range union and its human-readable label."""

    try:
        if options.weeks:
            return iso_week_union(options.weeks, timezone_value), ", ".join(options.weeks)
        if options.from_date is not None or options.to_date is not None:
            label = _calendar_range_label(options.from_date, options.to_date)
            return calendar_date_range(options.from_date, options.to_date, timezone_value), label
        if options.all_dates:
            return all_time_range(), "all available dates"
        local_now = now.astimezone(timezone_value)
        iso = local_now.isocalendar()
        return current_week_range(now, timezone_value), f"{iso.year:04d}-W{iso.week:02d}"
    except (TimeRangeError, ValueError) as error:
        raise UsageError(str(error)) from error


def resolve_schedule(options: RawOptions) -> Schedule:
    """Parse the configured working-hours expression."""

    try:
        return parse_schedule(options.hours)
    except ValueError as error:
        raise UsageError(str(error)) from error


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
