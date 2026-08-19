"""Immutable configuration types and built-in defaults."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from workfold.configuration.profiles import EventProfile, evidence_for_profile
from workfold.configuration.styles import DEFAULT_EVENT_STYLE_SHEET, EventStyleSheet
from workfold.domain.evidence import EvidenceKind, EvidenceSelection
from workfold.domain.observations import Weekday
from workfold.domain.schedule import Schedule
from workfold.domain.scope import RefScope
from workfold.folding.bands import ClusterAnchor


class UsageError(ValueError):
    """Raised when command-line values are individually or jointly invalid."""

    def __init__(
        self,
        message: str,
        *,
        setting_keys: tuple[str, ...] = (),
        include_setting_values: bool = False,
    ) -> None:
        super().__init__(message)
        if any(not key for key in setting_keys):
            raise ValueError("usage-error setting keys must not be empty")
        self.setting_keys = tuple(dict.fromkeys(setting_keys))
        self.include_setting_values = include_setting_values


class MarkerStyle(str, Enum):
    SOURCE = "source"
    IDENTITY = "identity"


class CountGrouping(str, Enum):
    """How exact-count tokens group busy-cell events."""

    EVENT = "event"
    VISUAL = "visual"


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


class BandLabel(str, Enum):
    """How a terminal time band is labeled."""

    RANGE = "range"
    START = "start"


class ListSchedule(str, Enum):
    """Optional schedule predicate applied to detailed event rows."""

    ALL = "all"
    INSIDE = "inside"
    OUTSIDE = "outside"


@dataclass(frozen=True, slots=True)
class EventListSelection:
    """A bounded detail projection over events already selected for the report."""

    schedule: ListSchedule = ListSchedule.ALL
    evidence_kinds: tuple[EvidenceKind, ...] = ()

    def __post_init__(self) -> None:
        _require_list_schedule(self.schedule)
        _require_list_evidence_kinds(tuple(self.evidence_kinds))
        canonical = tuple(kind for kind in EvidenceKind if kind in self.evidence_kinds)
        if canonical != self.evidence_kinds:
            raise ValueError("listed evidence kinds must be unique and canonically ordered")

    def includes_schedule_state(self, within_schedule: bool) -> bool:
        if self.schedule is ListSchedule.ALL:
            return True
        return within_schedule is (self.schedule is ListSchedule.INSIDE)


def _require_list_schedule(value: object) -> None:
    if not isinstance(value, ListSchedule):
        raise TypeError("listed schedule must be a ListSchedule")


def _require_list_evidence_kinds(values: tuple[object, ...]) -> None:
    if any(not isinstance(kind, EvidenceKind) for kind in values):
        raise TypeError("listed evidence kinds must be EvidenceKind values")


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
class TerminalPreferences:
    """Validated user preferences consumed only by the terminal boundary."""

    marker_style: MarkerStyle
    grid_style: GridStyle
    band_label: BandLabel
    show_empty_bands: bool
    no_color: bool
    event_list: EventListSelection | None
    event_limit: int
    coverage: bool
    strict: bool
    verbose: bool
    count_grouping: CountGrouping = CountGrouping.EVENT
    event_styles: EventStyleSheet = DEFAULT_EVENT_STYLE_SHEET


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Validated settings for one Workfold invocation."""

    paths: tuple[Path, ...]
    weeks: tuple[str, ...]
    from_date: date | None
    to_date: date | None
    all_dates: bool
    rolling_duration: RollingDuration | None
    profile: EventProfile | None
    evidence: EvidenceSelection
    ref_scope: RefScope
    git_identities: tuple[str, ...]
    include_ignored: bool
    respect_gitignore: bool
    exclusions: tuple[str, ...]
    schedule: Schedule
    timezone: ZoneInfo | None
    cluster_window: timedelta
    display_hours: DisplayHours | None
    hide_days: tuple[Weekday, ...]
    hide_empty_days: tuple[Weekday, ...]
    terminal: TerminalPreferences
    cluster_anchor: ClusterAnchor = ClusterAnchor.EVENT

    def __post_init__(self) -> None:
        if self.profile is not None and self.evidence != evidence_for_profile(self.profile):
            raise ValueError("named event profile does not match the resolved evidence selection")


@dataclass(frozen=True, slots=True)
class UnresolvedOptions:
    """Typed setting values awaiting cross-option validation."""

    paths: tuple[Path, ...]
    time_selectors: tuple[str, ...]
    profiles: tuple[str, ...]
    event_selectors: tuple[str, ...] | None
    commits_from: str | None
    git_identities: tuple[str, ...]
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
    list_selectors: tuple[str, ...] | None
    limit: int
    coverage: bool
    strict: bool
    verbose: bool
    explicit_names: frozenset[str] = frozenset()
    cluster_anchor: str = ClusterAnchor.EVENT.value
    band_label: str = BandLabel.RANGE.value
    show_empty_bands: bool = False
    count_grouping: str = CountGrouping.EVENT.value


DEFAULT_HOURS = "Mo-Fr 08:00-16:30"
DEFAULT_CLUSTER_WINDOW = timedelta(hours=1)


__all__ = [
    "DEFAULT_CLUSTER_WINDOW",
    "DEFAULT_HOURS",
    "CountGrouping",
    "DisplayHours",
    "EventListSelection",
    "GridStyle",
    "ListSchedule",
    "MarkerStyle",
    "RunOptions",
    "RefScope",
    "RollingDuration",
    "TerminalPreferences",
    "UnresolvedOptions",
    "UsageError",
]
