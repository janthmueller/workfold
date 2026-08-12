"""Command-line configuration and option compatibility."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum, IntFlag
from pathlib import Path

from workfold import time_bands
from workfold.models import Weekday
from workfold.time_bands import BandLabel, ClusterAnchor


class UsageError(ValueError):
    """Raised when command-line values are individually or jointly invalid."""


class SourceMode(str, Enum):
    GIT = "git"
    FILESYSTEM = "fs"
    BOTH = "both"

    @property
    def includes_git(self) -> bool:
        return self in {SourceMode.GIT, SourceMode.BOTH}

    @property
    def includes_filesystem(self) -> bool:
        return self in {SourceMode.FILESYSTEM, SourceMode.BOTH}


class CollectionProfile(str, Enum):
    STANDARD = "standard"
    PORTABLE = "portable"
    FULL = "full"


class MarkerStyle(str, Enum):
    SOURCE = "source"
    IDENTITY = "identity"


class GridStyle(str, Enum):
    NONE = "none"
    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"
    BOTH = "both"

    @property
    def has_vertical_lines(self) -> bool:
        """Return whether weekday columns have vertical separators."""

        return self in {GridStyle.VERTICAL, GridStyle.BOTH}

    @property
    def has_horizontal_lines(self) -> bool:
        """Return whether logical time clusters have horizontal separators."""

        return self in {GridStyle.HORIZONTAL, GridStyle.BOTH}


class GitMode(str, Enum):
    COMMITS = "commits"
    FILES = "files"
    BOTH = "both"

    @property
    def includes_commit_markers(self) -> bool:
        return self in {GitMode.COMMITS, GitMode.BOTH}

    @property
    def includes_file_changes(self) -> bool:
        return self in {GitMode.FILES, GitMode.BOTH}


class GitDateMode(str, Enum):
    AUTHOR = "author"
    COMMITTER = "committer"
    BOTH = "both"


class GitRecords(IntFlag):
    """Resolved Git record families; commit/file-change granularity is separate."""

    COMMITS = 1
    TAGS = 2
    REFLOGS = 4
    ALL = COMMITS | TAGS | REFLOGS

    @property
    def includes_commits(self) -> bool:
        return bool(self & GitRecords.COMMITS)

    @property
    def includes_tags(self) -> bool:
        return bool(self & GitRecords.TAGS)

    @property
    def includes_reflogs(self) -> bool:
        return bool(self & GitRecords.REFLOGS)


class RefScope(str, Enum):
    HEAD = "head"
    LOCAL_BRANCHES = "local-branches"
    ALL_REFS = "all-refs"


class FilesystemTime(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    CHANGED = "changed"
    ACCESSED = "accessed"


class FilesystemEntry(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class DisplayHours:
    """A half-open wall-clock display range in minutes since midnight."""

    start_minute: int
    end_minute: int


@dataclass(frozen=True, slots=True)
class RollingDuration:
    """A fixed elapsed duration selected relative to one captured clock value."""

    duration: timedelta
    label: str

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError("a rolling duration must be positive")
        if not self.label:
            raise ValueError("a rolling duration label must not be empty")


@dataclass(frozen=True, slots=True)
class RawOptions:
    """Validated CLI values before environment/timezone resolution."""

    paths: tuple[Path, ...]
    weeks: tuple[str, ...]
    from_date: date | None
    to_date: date | None
    all_dates: bool
    rolling_duration: RollingDuration | None
    profile: CollectionProfile
    source: SourceMode
    git_mode: GitMode
    git_date: GitDateMode
    git_records: GitRecords
    ref_scope: RefScope
    git_identities: tuple[str, ...]
    filesystem_times: tuple[FilesystemTime, ...]
    filesystem_entries: tuple[FilesystemEntry, ...]
    include_ignored: bool
    respect_gitignore: bool
    exclusions: tuple[str, ...]
    hours: str
    timezone_name: str | None
    cluster_window: timedelta
    marker_style: MarkerStyle
    grid_style: GridStyle
    display_hours: DisplayHours | None
    hide_days: tuple[Weekday, ...]
    hide_empty_days: tuple[Weekday, ...]
    no_color: bool
    list_outside: bool
    limit: int
    coverage: bool
    strict: bool
    verbose: bool
    cluster_anchor: ClusterAnchor = ClusterAnchor.EVENT
    band_label: BandLabel = BandLabel.RANGE
    show_empty_bands: bool = False


@dataclass(frozen=True, slots=True)
class UnresolvedOptions:
    """Typed, presentation-agnostic inputs awaiting domain validation."""

    paths: tuple[Path, ...]
    time_selectors: tuple[str, ...]
    modes: tuple[str, ...]
    profiles: tuple[str, ...]
    git_records: str | None
    commit_times: str | None
    commits_from: str | None
    git_identities: tuple[str, ...]
    filesystem_times: str | None
    filesystem_entries: str | None
    include_ignored: bool
    exclusions: tuple[str, ...]
    hours: str
    timezone_name: str | None
    cluster_window: str
    marker_style: str
    grid_style: str
    display_hours: str | None
    hide_days: tuple[str, ...]
    hide_empty_days: tuple[str, ...]
    no_color: bool
    list_outside: bool
    limit: int
    coverage: bool
    strict: bool
    verbose: bool
    explicit_names: frozenset[str] = frozenset()
    cluster_anchor: str = ClusterAnchor.EVENT.value
    band_label: str = BandLabel.RANGE.value
    show_empty_bands: bool = False


_ISO_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>[0-9]{2})$")
_TIME = re.compile(r"^(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})$")
_DURATION_PART = re.compile(r"(?P<amount>[0-9]+)(?P<unit>[hms])")
_ROLLING_DURATION_PART = re.compile(r"(?P<amount>[0-9]+)(?P<unit>[wdhm])")
DEFAULT_HOURS = "Mo-Fr 08:00-16:30"
DEFAULT_CLUSTER_WINDOW = timedelta(hours=1)
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
    selectors = tuple(values)
    if not selectors:
        selectors = ("this-week",)
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


def _validate_cluster_window_anchor(cluster_window: timedelta, cluster_anchor: ClusterAnchor) -> None:
    try:
        time_bands.validate_cluster_window_alignment(cluster_window, cluster_anchor)
    except ValueError as error:
        raise UsageError(
            "--cluster-anchor midnight requires --cluster-window to use whole minutes "
            "so fixed HH:MM band labels remain exact"
        ) from error


def _validate_empty_band_mode(show_empty_bands: bool, cluster_anchor: ClusterAnchor) -> None:
    if show_empty_bands and cluster_anchor is not ClusterAnchor.MIDNIGHT:
        raise UsageError("--show-empty-bands requires --cluster-anchor midnight")


def _parse_ordered_duration(
    value: str,
    *,
    pattern: re.Pattern[str],
    unit_seconds: dict[str, int],
    reject_zero_parts: bool,
) -> tuple[timedelta, str] | None:
    """Parse complete ordered components for public duration grammars."""

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


def _explicit(values: UnresolvedOptions, name: str) -> bool:
    return name in values.explicit_names


def resolve_options(values: UnresolvedOptions) -> RawOptions:
    """Validate typed setting values and expand collection profiles."""
    weeks, from_date, to_date, all_dates, rolling_duration = parse_time_selectors(values.time_selectors)
    modes = values.modes
    if len(modes) > 1:
        raise UsageError("--mode may be supplied only once")
    mode = modes[0] if modes else "git"
    profiles = values.profiles
    if len(profiles) > 1:
        raise UsageError("--profile may be supplied only once")
    profile = CollectionProfile(profiles[0] if profiles else CollectionProfile.STANDARD.value)
    source = {
        "git": SourceMode.GIT,
        "fs": SourceMode.FILESYSTEM,
        "all": SourceMode.BOTH,
    }[mode]

    if profile is CollectionProfile.PORTABLE and source is not SourceMode.GIT:
        raise UsageError("--profile portable is valid only with --mode git")

    preset = None if profile is CollectionProfile.STANDARD else f"--profile {profile.value}"
    controlled = {
        "--git-records": "git_records",
        "--git-commit-times": "commit_times",
        "--git-commits-from": "commits_from",
        "--fs-times": "filesystem_times",
        "--fs-entries": "filesystem_entries",
        "--include-ignored/--respect-gitignore": "include_ignored",
    }
    if preset is not None:
        conflicts = [flag for flag, name in controlled.items() if _explicit(values, name)]
        if conflicts:
            raise UsageError(f"{preset} controls {', '.join(conflicts)}; remove the scope option(s)")

    if profile is CollectionProfile.FULL:
        git_mode = GitMode.BOTH if source.includes_git else GitMode.COMMITS
        git_date = GitDateMode.BOTH if source.includes_git else GitDateMode.AUTHOR
        git_records = GitRecords.ALL if source.includes_git else GitRecords.COMMITS
        filesystem_times = (
            tuple(FilesystemTime)
            if source.includes_filesystem
            else (
                FilesystemTime.CREATED,
                FilesystemTime.MODIFIED,
            )
        )
        filesystem_entries = tuple(FilesystemEntry) if source.includes_filesystem else (FilesystemEntry.FILE,)
    elif profile is CollectionProfile.PORTABLE:
        git_mode = GitMode.COMMITS
        git_date = GitDateMode.BOTH
        git_records = GitRecords.COMMITS | GitRecords.TAGS
        filesystem_times = (FilesystemTime.CREATED, FilesystemTime.MODIFIED)
        filesystem_entries = (FilesystemEntry.FILE,)
    else:
        git_records_value = values.git_records if values.git_records is not None else "commit"
        commit_times_value = values.commit_times if values.commit_times is not None else "author"
        filesystem_times_value = values.filesystem_times if values.filesystem_times is not None else "birth,modified"
        filesystem_entries_value = values.filesystem_entries if values.filesystem_entries is not None else "file"
        git_mode, git_records = parse_git_records(git_records_value)
        git_date = parse_commit_times(commit_times_value)
        filesystem_times = parse_filesystem_times(filesystem_times_value)
        filesystem_entries = parse_filesystem_entries(filesystem_entries_value)

    if values.commits_from is not None:
        ref_scope = {
            "head": RefScope.HEAD,
            "local-branches": RefScope.LOCAL_BRANCHES,
            "all-refs": RefScope.ALL_REFS,
        }[values.commits_from]
    elif profile in {CollectionProfile.PORTABLE, CollectionProfile.FULL}:
        ref_scope = RefScope.ALL_REFS
    else:
        ref_scope = RefScope.LOCAL_BRANCHES

    explicit_git = any(_explicit(values, name) for name in ("git_records", "commit_times", "commits_from")) or bool(
        values.git_identities
    )
    explicit_filesystem = any(
        _explicit(values, name) for name in ("filesystem_times", "filesystem_entries", "include_ignored")
    ) or bool(values.exclusions)
    if source is SourceMode.FILESYSTEM and explicit_git:
        raise UsageError("Git-specific options cannot be used with --mode fs")
    if source is SourceMode.GIT and explicit_filesystem:
        raise UsageError("filesystem-specific options cannot be used with --mode git")

    if not git_records.includes_commits:
        irrelevant: list[str] = []
        if _explicit(values, "commit_times"):
            irrelevant.append("--git-commit-times")
        if _explicit(values, "commits_from"):
            irrelevant.append("--git-commits-from")
        if irrelevant:
            raise UsageError(f"{', '.join(irrelevant)} require commit or file-change records to be enabled")

    git_identities = tuple(value.strip() for value in values.git_identities)
    if any(not value for value in git_identities):
        raise UsageError("--git-identity values cannot be empty")
    exclusions = tuple(value.strip() for value in values.exclusions)
    if any(not value for value in exclusions):
        raise UsageError("--exclude values cannot be empty")
    if any(value.startswith("!") for value in exclusions):
        raise UsageError("--exclude patterns cannot be negated; explicit exclusions always win")

    if _explicit(values, "limit") and not values.list_outside:
        raise UsageError("--limit requires --list-outside")
    limit = values.limit
    if limit < 1:
        raise UsageError("--limit must be at least 1")

    include_ignored = bool((profile is CollectionProfile.FULL and source.includes_filesystem) or values.include_ignored)
    respect_gitignore = not include_ignored

    hide_days = parse_weekday_scopes(values.hide_days, option="--hide-days")
    if hide_days == tuple(Weekday):
        raise UsageError("--hide-days cannot hide all seven weekday columns")
    hide_empty_days = parse_weekday_scopes(values.hide_empty_days, option="--hide-empty-days")

    cluster_window = parse_cluster_window(values.cluster_window)
    cluster_anchor = ClusterAnchor(values.cluster_anchor)
    _validate_cluster_window_anchor(cluster_window, cluster_anchor)
    _validate_empty_band_mode(values.show_empty_bands, cluster_anchor)

    paths = values.paths or (Path("."),)
    return RawOptions(
        paths=paths,
        weeks=weeks,
        from_date=from_date,
        to_date=to_date,
        all_dates=all_dates,
        rolling_duration=rolling_duration,
        profile=profile,
        source=source,
        git_mode=git_mode,
        git_date=git_date,
        git_records=git_records,
        ref_scope=ref_scope,
        git_identities=git_identities,
        filesystem_times=filesystem_times,
        filesystem_entries=filesystem_entries,
        include_ignored=include_ignored,
        respect_gitignore=respect_gitignore,
        exclusions=exclusions,
        hours=values.hours or DEFAULT_HOURS,
        timezone_name=values.timezone_name,
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        band_label=BandLabel(values.band_label),
        show_empty_bands=values.show_empty_bands,
        marker_style=MarkerStyle(values.marker_style),
        grid_style=GridStyle(values.grid_style),
        display_hours=parse_display_hours(values.display_hours) if values.display_hours else None,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
        no_color=values.no_color,
        list_outside=values.list_outside,
        limit=limit,
        coverage=values.coverage,
        strict=values.strict,
        verbose=values.verbose,
    )
