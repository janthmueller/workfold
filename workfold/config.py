"""Command-line configuration and option compatibility."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum, IntFlag
from pathlib import Path


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
    ALL = "all"


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
class RawOptions:
    """Validated CLI values before environment/timezone resolution."""

    paths: tuple[Path, ...]
    weeks: tuple[str, ...]
    from_date: date | None
    to_date: date | None
    all_dates: bool
    profile: CollectionProfile
    source: SourceMode
    git_mode: GitMode
    git_date: GitDateMode
    git_records: GitRecords
    ref_scope: RefScope
    authors: tuple[str, ...]
    filesystem_times: tuple[FilesystemTime, ...]
    filesystem_entries: tuple[FilesystemEntry, ...]
    include_ignored: bool
    respect_gitignore: bool
    exclusions: tuple[str, ...]
    hours: str
    timezone_name: str | None
    cluster_window: timedelta
    display_hours: DisplayHours | None
    no_color: bool
    list_outside: bool
    limit: int
    coverage: bool
    strict: bool
    verbose: bool


_ISO_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>[0-9]{2})$")
_TIME = re.compile(r"^(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})$")
_DURATION_PART = re.compile(r"(?P<amount>[0-9]+)(?P<unit>[hms])")
DEFAULT_HOURS = "Mo-Fr 08:00-16:30"
DEFAULT_CLUSTER_WINDOW = timedelta(hours=1)
_MAX_CLUSTER_WINDOW = timedelta(days=1)


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
) -> tuple[tuple[str, ...], date | None, date | None, bool]:
    """Resolve public time selectors into the application's date-range fields."""
    selectors = tuple(values)
    if not selectors:
        selectors = ("this-week",)
    if len(selectors) > 1:
        if any(_ISO_WEEK.fullmatch(value) is None for value in selectors):
            raise UsageError("--time may be repeated only to form a union of ISO weeks")
        weeks = tuple(dict.fromkeys(validate_iso_week(value) for value in selectors))
        return weeks, None, None, False

    selector = next(iter(selectors))
    if selector == "this-week":
        return (), None, None, False
    if selector == "all":
        return (), None, None, True
    if _ISO_WEEK.fullmatch(selector) is not None:
        return (validate_iso_week(selector),), None, None, False
    if selector.count("..") == 1:
        start_text, end_text = selector.split("..", maxsplit=1)
        if not start_text and not end_text:
            raise UsageError("--time '..' is empty; use --time all for an unbounded range")
        from_date = _parse_date(start_text, "--time range start") if start_text else None
        to_date = _parse_date(end_text, "--time range end") if end_text else None
        if from_date is not None and to_date is not None and from_date > to_date:
            raise UsageError("--time range start cannot be after its end")
        return (), from_date, to_date, False
    raise UsageError(
        "--time must be this-week, all, an ISO week (YYYY-Www), or an inclusive date range "
        "(START..END, START.., or ..END)"
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


def parse_cluster_window(value: str) -> timedelta:
    """Parse an ordered, positive h/m/s duration shorter than one day."""
    text = value.strip()
    cursor = 0
    total_seconds = 0
    previous_unit_order = -1
    unit_order = {"h": 0, "m": 1, "s": 2}
    unit_seconds = {"h": 60 * 60, "m": 60, "s": 1}
    found_part = False

    for match in _DURATION_PART.finditer(text):
        separator = text[cursor : match.start()]
        if cursor == 0:
            if separator:
                break
        elif separator and not separator.isspace():
            break

        unit = match.group("unit")
        order = unit_order[unit]
        if order <= previous_unit_order:
            break
        previous_unit_order = order
        try:
            amount = int(match.group("amount"))
        except ValueError:
            break
        total_seconds += amount * unit_seconds[unit]
        cursor = match.end()
        found_part = True
    else:
        if found_part and cursor == len(text) and 0 < total_seconds < int(_MAX_CLUSTER_WINDOW.total_seconds()):
            return timedelta(seconds=total_seconds)

    raise UsageError(
        "--cluster-window must be a positive duration shorter than 24h with ordered h, m, and s units "
        "(for example 10m or '1h 5m')"
    )


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


def _explicit(namespace: argparse.Namespace, name: str) -> bool:
    return getattr(namespace, name) is not None


def options_from_namespace(namespace: argparse.Namespace) -> RawOptions:
    """Validate argparse output and expand defaults/presets."""
    weeks, from_date, to_date, all_dates = parse_time_selectors(namespace.time_selectors)
    modes = tuple(namespace.modes)
    if len(modes) > 1:
        raise UsageError("--mode may be supplied only once")
    mode = modes[0] if modes else "git"
    profiles = tuple(namespace.profiles)
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
        "--git-records": namespace.git_records,
        "--git-commit-times": namespace.commit_times,
        "--git-commits-from": namespace.commits_from,
        "--fs-times": namespace.filesystem_times,
        "--fs-entries": namespace.filesystem_entries,
        "--respect-gitignore": namespace.respect_gitignore,
        "--include-ignored": namespace.include_ignored,
    }
    if preset is not None:
        conflicts = [flag for flag, value in controlled.items() if value is not None]
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
        git_records_value = namespace.git_records if namespace.git_records is not None else "commit"
        commit_times_value = namespace.commit_times if namespace.commit_times is not None else "author"
        filesystem_times_value = (
            namespace.filesystem_times if namespace.filesystem_times is not None else "birth,modified"
        )
        filesystem_entries_value = namespace.filesystem_entries if namespace.filesystem_entries is not None else "file"
        git_mode, git_records = parse_git_records(git_records_value)
        git_date = parse_commit_times(commit_times_value)
        filesystem_times = parse_filesystem_times(filesystem_times_value)
        filesystem_entries = parse_filesystem_entries(filesystem_entries_value)

    ref_scope = RefScope.HEAD if namespace.commits_from == "HEAD" else RefScope.ALL

    explicit_git = any(_explicit(namespace, name) for name in ("git_records", "commit_times", "commits_from")) or bool(
        namespace.authors
    )
    explicit_filesystem = any(
        (
            _explicit(namespace, "filesystem_times"),
            _explicit(namespace, "filesystem_entries"),
            _explicit(namespace, "respect_gitignore"),
            _explicit(namespace, "include_ignored"),
            bool(namespace.exclusions),
        )
    )
    if source is SourceMode.FILESYSTEM and explicit_git:
        raise UsageError("Git-specific options cannot be used with --mode fs")
    if source is SourceMode.GIT and explicit_filesystem:
        raise UsageError("filesystem-specific options cannot be used with --mode git")

    if not git_records.includes_commits:
        irrelevant: list[str] = []
        if _explicit(namespace, "commit_times"):
            irrelevant.append("--git-commit-times")
        if _explicit(namespace, "commits_from"):
            irrelevant.append("--git-commits-from")
        if namespace.authors:
            irrelevant.append("--author")
        if irrelevant:
            raise UsageError(f"{', '.join(irrelevant)} require commit or file-change records to be enabled")

    authors = tuple(value.strip() for value in namespace.authors)
    if any(not value for value in authors):
        raise UsageError("--author values cannot be empty")
    exclusions = tuple(value.strip() for value in namespace.exclusions)
    if any(not value for value in exclusions):
        raise UsageError("--exclude values cannot be empty")
    if any(value.startswith("!") for value in exclusions):
        raise UsageError("--exclude patterns cannot be negated; explicit exclusions always win")

    if namespace.limit is not None and not namespace.list_outside:
        raise UsageError("--limit requires --list-outside")
    limit = namespace.limit if namespace.limit is not None else 50
    if limit < 1:
        raise UsageError("--limit must be at least 1")

    if namespace.respect_gitignore and namespace.include_ignored:
        raise UsageError("--respect-gitignore and --include-ignored are mutually exclusive")
    include_ignored = bool(
        (profile is CollectionProfile.FULL and source.includes_filesystem) or namespace.include_ignored
    )
    respect_gitignore = not include_ignored

    paths = tuple(Path(value) for value in namespace.paths) or (Path("."),)
    return RawOptions(
        paths=paths,
        weeks=weeks,
        from_date=from_date,
        to_date=to_date,
        all_dates=all_dates,
        profile=profile,
        source=source,
        git_mode=git_mode,
        git_date=git_date,
        git_records=git_records,
        ref_scope=ref_scope,
        authors=authors,
        filesystem_times=filesystem_times,
        filesystem_entries=filesystem_entries,
        include_ignored=include_ignored,
        respect_gitignore=respect_gitignore,
        exclusions=exclusions,
        hours=namespace.hours or DEFAULT_HOURS,
        timezone_name=namespace.timezone_name,
        cluster_window=parse_cluster_window(namespace.cluster_window),
        display_hours=parse_display_hours(namespace.display_hours) if namespace.display_hours else None,
        no_color=bool(namespace.no_color),
        list_outside=bool(namespace.list_outside),
        limit=limit,
        coverage=bool(namespace.coverage),
        strict=bool(namespace.strict),
        verbose=bool(namespace.verbose),
    )
