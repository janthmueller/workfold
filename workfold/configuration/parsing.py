"""Pure parsers and local validation for configuration values."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta

from workfold.configuration.options import (
    DisplayHours,
    FilesystemEntry,
    FilesystemTime,
    GitDateMode,
    GitMode,
    GitRecords,
    RollingDuration,
    UsageError,
)
from workfold.domain.observations import Weekday
from workfold.folding import bands as time_bands
from workfold.folding.bands import ClusterAnchor

_ISO_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>[0-9]{2})$")
_TIME = re.compile(r"^(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})$")
_DURATION_PART = re.compile(r"(?P<amount>[0-9]+)(?P<unit>[hms])")
_ROLLING_DURATION_PART = re.compile(r"(?P<amount>[0-9]+)(?P<unit>[wdhm])")
_MAX_CLUSTER_WINDOW = timedelta(days=1)
_WEEKDAY_ALIASES: dict[str, Weekday] = {
    "mo": Weekday.MONDAY,
    "mon": Weekday.MONDAY,
    "monday": Weekday.MONDAY,
    "tu": Weekday.TUESDAY,
    "tue": Weekday.TUESDAY,
    "tuesday": Weekday.TUESDAY,
    "we": Weekday.WEDNESDAY,
    "wed": Weekday.WEDNESDAY,
    "wednesday": Weekday.WEDNESDAY,
    "th": Weekday.THURSDAY,
    "thu": Weekday.THURSDAY,
    "thursday": Weekday.THURSDAY,
    "fr": Weekday.FRIDAY,
    "fri": Weekday.FRIDAY,
    "friday": Weekday.FRIDAY,
    "sa": Weekday.SATURDAY,
    "sat": Weekday.SATURDAY,
    "saturday": Weekday.SATURDAY,
    "su": Weekday.SUNDAY,
    "sun": Weekday.SUNDAY,
    "sunday": Weekday.SUNDAY,
}
_WEEKDAYS = tuple(day for day in Weekday if not day.is_weekend)
_WEEKEND = tuple(day for day in Weekday if day.is_weekend)
_WEEKDAY_SCOPES = {
    "weekday": _WEEKDAYS,
    "weekdays": _WEEKDAYS,
    "weekend": _WEEKEND,
    "weekends": _WEEKEND,
    "all": tuple(Weekday),
}


def _parse_date(value: str, option: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise UsageError(f"{option} must be a valid date in YYYY-MM-DD form: {value!r}") from error
    if parsed.isoformat() != value:
        raise UsageError(f"{option} must be a valid date in YYYY-MM-DD form: {value!r}")
    return parsed


def validate_iso_week(value: str) -> str:
    """Validate and return a canonical ISO week selector."""

    match = _ISO_WEEK.fullmatch(value)
    if match is None:
        raise UsageError(f"--time ISO weeks must use YYYY-Www form: {value!r}")
    year = int(match.group("year"))
    week = int(match.group("week"))
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as error:
        raise UsageError(f"--time is not a valid ISO week: {value!r}") from error
    return f"{year:04d}-W{week:02d}"


def parse_time_selectors(
    values: Sequence[str],
) -> tuple[tuple[str, ...], date | None, date | None, bool, RollingDuration | None]:
    """Resolve public time selectors into the application's date-range fields."""

    selectors = tuple(values) or ("this-week",)
    if len(selectors) > 1:
        if any(_ISO_WEEK.fullmatch(value) is None for value in selectors):
            raise UsageError("--time may be repeated only to form a union of ISO weeks")
        weeks = tuple(dict.fromkeys(validate_iso_week(value) for value in selectors))
        return weeks, None, None, False, None

    selector = next(iter(selectors))
    if selector == "this-week":
        return (), None, None, False, None
    if selector == "all":
        return (), None, None, True, None
    if _ISO_WEEK.fullmatch(selector) is not None:
        return (validate_iso_week(selector),), None, None, False, None
    if selector.count("..") == 1:
        start_text, end_text = selector.split("..", maxsplit=1)
        if not start_text and not end_text:
            raise UsageError("--time '..' is empty; use --time all for an unbounded range")
        from_date = _parse_date(start_text, "--time range start") if start_text else None
        to_date = _parse_date(end_text, "--time range end") if end_text else None
        if from_date is not None and to_date is not None and from_date > to_date:
            raise UsageError("--time range start cannot be after its end")
        return (), from_date, to_date, False, None
    if any(unit in selector for unit in "wdhm"):
        return (), None, None, False, parse_rolling_duration(selector)
    raise UsageError(
        "--time must be this-week, all, an ISO week (YYYY-Www), a rolling duration "
        "(for example 2w3d or 6h30m), or an inclusive date range "
        "(START..END, START.., or ..END)"
    )


def parse_rolling_duration(value: str) -> RollingDuration:
    """Parse an ordered fixed duration using weeks, days, hours, and minutes."""

    parsed = _parse_ordered_duration(
        value,
        pattern=_ROLLING_DURATION_PART,
        unit_seconds={"w": 7 * 86_400, "d": 86_400, "h": 3_600, "m": 60},
        reject_zero_parts=True,
    )
    if parsed is not None:
        duration, label = parsed
        return RollingDuration(duration, label)
    raise UsageError(
        "--time rolling durations must be positive and use ordered w, d, h, and m units (for example 2w3d or 6h30m)"
    )


def parse_clock_minutes(value: str, *, allow_24: bool) -> int:
    """Parse HH:MM into minutes since midnight."""

    match = _TIME.fullmatch(value)
    if match is None:
        raise UsageError(f"time must use HH:MM form: {value!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if minute >= 60 or hour > 24 or (hour == 24 and (minute != 0 or not allow_24)):
        raise UsageError(f"invalid wall-clock time: {value!r}")
    return hour * 60 + minute


def parse_display_hours(value: str) -> DisplayHours:
    """Parse a non-overnight display range."""

    try:
        start_text, end_text = value.split("-", maxsplit=1)
    except ValueError as error:
        raise UsageError("--display-hours must use HH:MM-HH:MM form") from error
    start = parse_clock_minutes(start_text, allow_24=False)
    end = parse_clock_minutes(end_text, allow_24=True)
    if start >= end:
        raise UsageError("--display-hours must end after it starts; overnight ranges are not supported")
    return DisplayHours(start, end)


def parse_weekday_scopes(values: Sequence[str], *, option: str) -> tuple[Weekday, ...]:
    """Expand repeated comma-separated weekday names and groups."""

    selected: set[Weekday] = set()
    for value in values:
        parts = [part.strip().casefold() for part in value.split(",")]
        if not parts or any(not part for part in parts):
            raise UsageError(f"{option} must contain one or more weekday scopes")
        for part in parts:
            days = _WEEKDAY_SCOPES.get(part)
            if days is not None:
                selected.update(days)
                continue
            try:
                selected.add(_WEEKDAY_ALIASES[part])
            except KeyError as error:
                raise UsageError(
                    f"unknown {option} value {part!r}; use all, weekdays, weekend, or a weekday name"
                ) from error
    return tuple(sorted(selected))


def parse_cluster_window(value: str) -> timedelta:
    """Parse an ordered h/m/s duration from one minute up to one day."""

    parsed = _parse_ordered_duration(
        value,
        pattern=_DURATION_PART,
        unit_seconds={"h": 3_600, "m": 60, "s": 1},
        reject_zero_parts=False,
    )
    if parsed is not None and timedelta(minutes=1) <= parsed[0] < _MAX_CLUSTER_WINDOW:
        return parsed[0]
    raise UsageError(
        "--cluster-window must be at least 1m and shorter than 24h with ordered h, m, and s units "
        "(for example 10m, 1m30s, or '1h 5m')"
    )


def validate_cluster_options(
    cluster_window: timedelta,
    cluster_anchor: ClusterAnchor,
    *,
    show_empty_bands: bool,
) -> None:
    """Validate option relationships tied to the clustering model."""

    try:
        time_bands.validate_cluster_window_alignment(cluster_window, cluster_anchor)
    except ValueError as error:
        raise UsageError(
            "--cluster-anchor midnight requires --cluster-window to use whole minutes "
            "so fixed HH:MM band labels remain exact"
        ) from error
    if show_empty_bands and cluster_anchor is not ClusterAnchor.MIDNIGHT:
        raise UsageError("--show-empty-bands requires --cluster-anchor midnight")


def _parse_ordered_duration(
    value: str,
    *,
    pattern: re.Pattern[str],
    unit_seconds: dict[str, int],
    reject_zero_parts: bool,
) -> tuple[timedelta, str] | None:
    text = value.strip()
    cursor = 0
    total_seconds = 0
    label_parts: list[str] = []
    previous_unit_order = -1
    unit_order = {unit: order for order, unit in enumerate(unit_seconds)}
    for match in pattern.finditer(text):
        separator = text[cursor : match.start()]
        if separator and not separator.isspace():
            return None
        unit = match.group("unit")
        amount = int(match.group("amount"))
        order = unit_order[unit]
        if order <= previous_unit_order or (reject_zero_parts and amount == 0):
            return None
        previous_unit_order = order
        total_seconds += amount * unit_seconds[unit]
        label_parts.append(f"{amount}{unit}")
        cursor = match.end()
    if not label_parts or cursor != len(text) or total_seconds <= 0:
        return None
    try:
        duration = timedelta(seconds=total_seconds)
    except OverflowError:
        return None
    return duration, "".join(label_parts)


def parse_filesystem_times(value: str) -> tuple[FilesystemTime, ...]:
    """Parse a comma-separated filesystem timestamp selection."""

    values = [part.strip().lower() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise UsageError("--fs-times must contain one or more timestamp kinds")
    names = {
        "birth": FilesystemTime.CREATED,
        "modified": FilesystemTime.MODIFIED,
        "metadata-changed": FilesystemTime.CHANGED,
        "accessed": FilesystemTime.ACCESSED,
    }
    parsed: list[FilesystemTime] = []
    for part in values:
        try:
            item = names[part]
        except KeyError as error:
            choices = ", ".join(names)
            raise UsageError(f"unknown --fs-times value {part!r}; choose from {choices}") from error
        if item not in parsed:
            parsed.append(item)
    return tuple(parsed)


def parse_filesystem_entries(value: str) -> tuple[FilesystemEntry, ...]:
    """Parse a comma-separated filesystem entry-type selection."""

    values = [part.strip().lower() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise UsageError("--fs-entries must contain one or more entry types")
    parsed: list[FilesystemEntry] = []
    for part in values:
        try:
            item = FilesystemEntry(part)
        except ValueError as error:
            choices = ", ".join(item.value for item in FilesystemEntry)
            raise UsageError(f"unknown --fs-entries value {part!r}; choose from {choices}") from error
        if item not in parsed:
            parsed.append(item)
    return tuple(parsed)


def parse_git_records(value: str) -> tuple[GitMode, GitRecords]:
    """Parse public Git record names into record families and commit granularity."""

    values = [part.strip().lower() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise UsageError("--git-records must contain one or more record kinds")
    choices = ("commit", "file-change", "tag", "reflog")
    unknown = next((part for part in values if part not in choices), None)
    if unknown is not None:
        raise UsageError(f"unknown --git-records value {unknown!r}; choose from {', '.join(choices)}")

    selected = frozenset(values)
    has_commits = "commit" in selected
    has_file_changes = "file-change" in selected
    if has_commits and has_file_changes:
        git_mode = GitMode.BOTH
    elif has_file_changes:
        git_mode = GitMode.FILES
    else:
        git_mode = GitMode.COMMITS

    records = GitRecords(0)
    if has_commits or has_file_changes:
        records |= GitRecords.COMMITS
    if "tag" in selected:
        records |= GitRecords.TAGS
    if "reflog" in selected:
        records |= GitRecords.REFLOGS
    return git_mode, records


def parse_commit_times(value: str) -> GitDateMode:
    """Parse commit timestamp roles."""

    values = [part.strip().lower() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise UsageError("--git-commit-times must contain one or more timestamp roles")
    unknown = next((part for part in values if part not in {"author", "committer"}), None)
    if unknown is not None:
        raise UsageError(f"unknown --git-commit-times value {unknown!r}; choose from author, committer")
    selected = frozenset(values)
    if selected == frozenset(("author", "committer")):
        return GitDateMode.BOTH
    return GitDateMode(next(iter(selected)))


__all__ = [
    "parse_clock_minutes",
    "parse_cluster_window",
    "parse_commit_times",
    "parse_display_hours",
    "parse_filesystem_entries",
    "parse_filesystem_times",
    "parse_git_records",
    "parse_rolling_duration",
    "parse_time_selectors",
    "parse_weekday_scopes",
    "validate_cluster_options",
    "validate_iso_week",
]
