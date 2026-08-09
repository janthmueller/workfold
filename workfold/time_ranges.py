"""Timezone-aware, half-open instant ranges for Workfold date selectors."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

NANOSECONDS_PER_SECOND = 1_000_000_000

_UTC = datetime_timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_ISO_WEEK_PATTERN = re.compile(r"(?P<year>\d{4})-W(?P<week>\d{2})\Z", re.IGNORECASE)


class TimeRangeError(ValueError):
    """Raised for invalid calendar selectors or timezone values."""


class LocalTimezoneResolutionError(TimeRangeError):
    """Raised when the OS local timezone cannot be resolved to an IANA zone."""


@dataclass(frozen=True, slots=True)
class InstantRange:
    """One half-open UTC epoch-nanosecond range; ``None`` means unbounded."""

    start_utc_ns: int | None
    end_utc_ns: int | None

    def __post_init__(self) -> None:
        if self.start_utc_ns is not None and self.end_utc_ns is not None and self.start_utc_ns >= self.end_utc_ns:
            raise TimeRangeError("an instant range must have start before end")

    def contains(self, instant_utc_ns: int) -> bool:
        """Return whether an instant is within this half-open range."""

        return (self.start_utc_ns is None or instant_utc_ns >= self.start_utc_ns) and (
            self.end_utc_ns is None or instant_utc_ns < self.end_utc_ns
        )


@dataclass(frozen=True, slots=True)
class InstantRangeUnion:
    """A normalized union of disjoint half-open ranges."""

    ranges: tuple[InstantRange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranges", _normalize_ranges(self.ranges))

    def contains(self, instant_utc_ns: int) -> bool:
        """Return whether any constituent range contains the instant."""

        return any(item.contains(instant_utc_ns) for item in self.ranges)

    def __contains__(self, instant_utc_ns: object) -> bool:
        """Support ``instant in ranges`` without accepting non-integer values."""

        return (
            isinstance(instant_utc_ns, int) and not isinstance(instant_utc_ns, bool) and self.contains(instant_utc_ns)
        )

    @property
    def is_unbounded(self) -> bool:
        """Return whether this union selects all representable instants."""

        return self.ranges == (InstantRange(None, None),)

    @property
    def is_empty(self) -> bool:
        """Return whether this union selects no instants."""

        return not self.ranges


def datetime_to_utc_ns(value: datetime) -> int:
    """Convert an aware datetime to exact integer UTC epoch nanoseconds."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeRangeError("datetime must be timezone-aware")
    try:
        utc_value = value.astimezone(_UTC)
        delta = utc_value - _EPOCH
    except (OverflowError, ValueError) as error:
        raise TimeRangeError("local calendar boundary lies outside the representable UTC datetime range") from error
    seconds = delta.days * 86_400 + delta.seconds
    return seconds * NANOSECONDS_PER_SECOND + delta.microseconds * 1_000


def utc_ns_to_datetime(instant_utc_ns: int, timezone: ZoneInfo = ZoneInfo("UTC")) -> datetime:
    """Convert integer UTC epoch nanoseconds to an aware datetime.

    Python's :class:`datetime` stores microseconds, so sub-microsecond remainder
    is intentionally floored.  The original integer remains authoritative in
    the observation model.
    """

    seconds, nanoseconds = divmod(instant_utc_ns, NANOSECONDS_PER_SECOND)
    utc_value = _EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1_000)
    return utc_value.astimezone(timezone)


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve one IANA timezone name with historical transition rules."""

    normalized = name.strip()
    if not normalized:
        raise TimeRangeError("timezone name must not be empty")
    try:
        return ZoneInfo(normalized)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise TimeRangeError(f"unknown IANA timezone: {name}") from error


def resolve_local_timezone(
    *,
    environ: Mapping[str, str] | None = None,
    timezone_file: Path = Path("/etc/timezone"),
    localtime_file: Path = Path("/etc/localtime"),
) -> ZoneInfo:
    """Resolve the OS local zone without falling back to a fixed UTC offset."""

    environment = os.environ if environ is None else environ
    configured = environment.get("TZ", "").removeprefix(":").strip()
    candidates: list[str] = []
    if configured and not os.path.isabs(configured):
        candidates.append(configured)

    tzlocal_name = _tzlocal_zone_name()
    if tzlocal_name:
        candidates.append(tzlocal_name)

    try:
        timezone_file_name = timezone_file.read_text(encoding="utf-8").strip()
    except OSError:
        timezone_file_name = ""
    if timezone_file_name:
        candidates.append(timezone_file_name)

    try:
        symlink_target = os.readlink(localtime_file)
    except OSError:
        symlink_target = ""
    zoneinfo_marker = "zoneinfo/"
    if zoneinfo_marker in symlink_target:
        candidates.append(symlink_target.split(zoneinfo_marker, maxsplit=1)[1])

    current_zone = datetime.now().astimezone().tzinfo
    if isinstance(current_zone, ZoneInfo) and current_zone.key:
        candidates.append(current_zone.key)

    for candidate in dict.fromkeys(candidates):
        try:
            return resolve_timezone(candidate)
        except TimeRangeError:
            continue
    raise LocalTimezoneResolutionError(
        "could not resolve a DST-capable local timezone; supply --timezone with an IANA zone"
    )


def parse_iso_week(value: str) -> tuple[int, int]:
    """Parse and validate the canonical ``YYYY-Www`` ISO-week form."""

    match = _ISO_WEEK_PATTERN.fullmatch(value.strip())
    if match is None:
        raise TimeRangeError(f"invalid ISO week {value!r}; expected YYYY-Www")
    year = int(match.group("year"))
    week = int(match.group("week"))
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as error:
        raise TimeRangeError(f"invalid ISO week: {value}") from error
    return year, week


def iso_week_range(value: str, timezone: ZoneInfo) -> InstantRange:
    """Resolve one ISO week using local Monday boundaries in ``timezone``."""

    year, week = parse_iso_week(value)
    monday = date.fromisocalendar(year, week, 1)
    try:
        following_monday = monday + timedelta(days=7)
    except OverflowError as error:
        raise TimeRangeError(f"ISO week has no representable exclusive end: {value}") from error
    return _local_date_interval(monday, following_monday, timezone)


def iso_week_union(values: Iterable[str], timezone: ZoneInfo) -> InstantRangeUnion:
    """Resolve repeated ISO-week selectors into a normalized set union."""

    ranges = tuple(iso_week_range(value, timezone) for value in values)
    if not ranges:
        raise TimeRangeError("at least one ISO week is required")
    return InstantRangeUnion(ranges)


def calendar_date_range(
    start: date | None,
    end: date | None,
    timezone: ZoneInfo,
) -> InstantRangeUnion:
    """Resolve inclusive local calendar endpoints, allowing either to be open."""

    if start is not None and end is not None and start > end:
        raise TimeRangeError("--time range start must not be after its end")
    start_ns = _local_midnight_ns(start, timezone) if start is not None else None
    try:
        exclusive_end = end + timedelta(days=1) if end is not None else None
    except OverflowError as error:
        raise TimeRangeError("--time range end has no representable exclusive end") from error
    end_ns = _local_midnight_ns(exclusive_end, timezone) if exclusive_end is not None else None
    if start_ns is not None and end_ns is not None and start_ns == end_ns:
        # A timezone can skip an entire local calendar date (for example,
        # Pacific/Apia skipped 2011-12-30). The inclusive selector is valid,
        # but denotes no instants.
        return InstantRangeUnion(())
    return InstantRangeUnion((InstantRange(start_ns, end_ns),))


def current_week_range(now: datetime, timezone: ZoneInfo) -> InstantRangeUnion:
    """Resolve the current local ISO week using an injected aware clock value."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise TimeRangeError("current time must be timezone-aware")
    local_date = now.astimezone(timezone).date()
    monday = local_date - timedelta(days=local_date.weekday())
    try:
        following_monday = monday + timedelta(days=7)
    except OverflowError as error:
        raise TimeRangeError("current ISO week has no representable exclusive end") from error
    return InstantRangeUnion((_local_date_interval(monday, following_monday, timezone),))


def all_time_range() -> InstantRangeUnion:
    """Return an unbounded selector for all available timestamps."""

    return InstantRangeUnion((InstantRange(None, None),))


def parse_calendar_date(value: str) -> date:
    """Parse one strict ISO calendar date for CLI adapters."""

    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise TimeRangeError(f"invalid calendar date: {value}") from error
    if parsed.isoformat() != value:
        raise TimeRangeError(f"invalid calendar date: {value}")
    return parsed


def _local_date_interval(start: date, end: date, timezone: ZoneInfo) -> InstantRange:
    return InstantRange(_local_midnight_ns(start, timezone), _local_midnight_ns(end, timezone))


def _local_midnight_ns(value: date, timezone: ZoneInfo) -> int:
    return datetime_to_utc_ns(datetime(value.year, value.month, value.day, tzinfo=timezone))


def _normalize_ranges(ranges: tuple[InstantRange, ...]) -> tuple[InstantRange, ...]:
    if not ranges:
        return ()
    ordered = sorted(ranges, key=lambda item: (item.start_utc_ns is not None, item.start_utc_ns or 0))
    normalized: list[InstantRange] = []
    for candidate in ordered:
        if not normalized:
            normalized.append(candidate)
            continue
        current = normalized[-1]
        if current.end_utc_ns is None or candidate.start_utc_ns is None or candidate.start_utc_ns <= current.end_utc_ns:
            normalized[-1] = InstantRange(current.start_utc_ns, _later_end(current.end_utc_ns, candidate.end_utc_ns))
        else:
            normalized.append(candidate)
    return tuple(normalized)


def _later_end(first: int | None, second: int | None) -> int | None:
    if first is None or second is None:
        return None
    return max(first, second)


def _tzlocal_zone_name() -> str | None:
    try:
        return get_localzone_name()
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return None


__all__ = [
    "InstantRange",
    "InstantRangeUnion",
    "LocalTimezoneResolutionError",
    "NANOSECONDS_PER_SECOND",
    "TimeRangeError",
    "all_time_range",
    "calendar_date_range",
    "current_week_range",
    "datetime_to_utc_ns",
    "iso_week_range",
    "iso_week_union",
    "parse_calendar_date",
    "parse_iso_week",
    "resolve_local_timezone",
    "resolve_timezone",
    "utc_ns_to_datetime",
]
