"""Immutable configuration types and built-in defaults."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum, IntFlag
from pathlib import Path

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


DEFAULT_HOURS = "Mo-Fr 08:00-16:30"
DEFAULT_CLUSTER_WINDOW = timedelta(hours=1)


__all__ = [
    "DEFAULT_CLUSTER_WINDOW",
    "DEFAULT_HOURS",
    "CollectionProfile",
    "DisplayHours",
    "FilesystemEntry",
    "FilesystemTime",
    "GitDateMode",
    "GitMode",
    "GitRecords",
    "GridStyle",
    "MarkerStyle",
    "RawOptions",
    "RefScope",
    "RollingDuration",
    "SourceMode",
    "UnresolvedOptions",
    "UsageError",
]
