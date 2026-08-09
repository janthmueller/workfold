"""Working-schedule grammar, normalization, and marker classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from workfold.models import ActivityMarker, ClassifiedMarker, Weekday
from workfold.time_ranges import utc_ns_to_datetime

DEFAULT_SCHEDULE_TEXT = "Mo-Fr 08:00-16:30"

_DAY_TOKENS = {day.abbreviation.casefold(): day for day in Weekday}
_DAY_TOKENS.update({day.name[:3].casefold(): day for day in Weekday})
_CLAUSE_PATTERN = re.compile(r"(?P<days>[A-Za-z]{2,3}(?:\s*-\s*[A-Za-z]{2,3})?)\s+(?P<intervals>.+)\Z")
_INTERVAL_PATTERN = re.compile(
    r"(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>\d{2}:\d{2})\Z",
)


class ScheduleError(ValueError):
    """Raised when a working-schedule expression is invalid."""


@dataclass(frozen=True, slots=True, order=True)
class TimeInterval:
    """One half-open, same-day wall-clock interval in minutes."""

    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_minute < self.end_minute <= 1440:
            raise ScheduleError("schedule intervals require 00:00 <= start < end <= 24:00")

    def contains_minute(self, minute_of_day: int) -> bool:
        """Return whether a wall-clock minute falls inside this interval."""

        if not 0 <= minute_of_day < 1440:
            raise ValueError("minute_of_day must be between 0 and 1439")
        return self.start_minute <= minute_of_day < self.end_minute

    def __str__(self) -> str:
        return f"{_format_time(self.start_minute)}-{_format_time(self.end_minute)}"


@dataclass(frozen=True, slots=True)
class Schedule:
    """Normalized working intervals indexed Monday through Sunday."""

    intervals_by_weekday: tuple[tuple[TimeInterval, ...], ...]

    def __post_init__(self) -> None:
        if len(self.intervals_by_weekday) != len(Weekday):
            raise ScheduleError("a schedule must contain exactly seven weekday entries")
        normalized = tuple(_normalize_intervals(intervals) for intervals in self.intervals_by_weekday)
        object.__setattr__(self, "intervals_by_weekday", normalized)

    def intervals_for(self, weekday: Weekday) -> tuple[TimeInterval, ...]:
        """Return normalized intervals for one weekday."""

        return self.intervals_by_weekday[int(weekday)]

    def contains(self, weekday: Weekday, minute_of_day: int) -> bool:
        """Classify a local weekday/wall-clock minute using half-open bounds."""

        return any(interval.contains_minute(minute_of_day) for interval in self.intervals_for(weekday))

    def contains_local(self, value: datetime) -> bool:
        """Classify an aware localized datetime against this schedule."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("schedule classification requires an aware local datetime")
        weekday = Weekday(value.weekday())
        minute_of_day = value.hour * 60 + value.minute
        return self.contains(weekday, minute_of_day)

    def __str__(self) -> str:
        clauses: list[str] = []
        day_index = 0
        while day_index < len(Weekday):
            intervals = self.intervals_by_weekday[day_index]
            if not intervals:
                day_index += 1
                continue
            run_end = day_index
            while run_end + 1 < len(Weekday) and self.intervals_by_weekday[run_end + 1] == intervals:
                run_end += 1
            first = Weekday(day_index).abbreviation
            last = Weekday(run_end).abbreviation
            day_token = first if day_index == run_end else f"{first}-{last}"
            clauses.append(f"{day_token} {','.join(str(interval) for interval in intervals)}")
            day_index = run_end + 1
        return "; ".join(clauses)


def parse_schedule(value: str) -> Schedule:
    """Parse the documented schedule grammar and normalize its interval union."""

    if not value.strip():
        raise ScheduleError("schedule must not be empty")
    day_intervals: list[list[TimeInterval]] = [[] for _ in Weekday]
    clauses = value.split(";")
    for raw_clause in clauses:
        clause = raw_clause.strip()
        if not clause:
            raise ScheduleError("schedule contains an empty clause")
        match = _CLAUSE_PATTERN.fullmatch(clause)
        if match is None:
            raise ScheduleError(f"invalid schedule clause: {clause!r}")
        weekdays = _parse_day_set(match.group("days"))
        raw_intervals = match.group("intervals").split(",")
        if any(not item.strip() for item in raw_intervals):
            raise ScheduleError(f"invalid interval list in clause: {clause!r}")
        intervals = tuple(_parse_interval(item.strip()) for item in raw_intervals)
        for weekday in weekdays:
            day_intervals[int(weekday)].extend(intervals)
    return Schedule(tuple(tuple(intervals) for intervals in day_intervals))


def default_schedule() -> Schedule:
    """Return the personal-use MVP default schedule."""

    return parse_schedule(DEFAULT_SCHEDULE_TEXT)


def classify_marker(
    marker: ActivityMarker,
    timezone: ZoneInfo,
    schedule: Schedule,
) -> ClassifiedMarker:
    """Localize and schedule-classify a marker without chart layout."""

    local_datetime = utc_ns_to_datetime(marker.occurred_at_utc_ns, timezone)
    weekday = Weekday(local_datetime.weekday())
    minute_of_day = local_datetime.hour * 60 + local_datetime.minute
    return ClassifiedMarker(
        marker=marker,
        local_datetime=local_datetime,
        within_schedule=schedule.contains(weekday, minute_of_day),
    )


def _parse_day_set(value: str) -> tuple[Weekday, ...]:
    tokens = tuple(token.strip() for token in value.split("-"))
    if len(tokens) not in (1, 2):
        raise ScheduleError(f"invalid day set: {value!r}")
    weekdays: list[Weekday] = []
    for token in tokens:
        try:
            weekdays.append(_DAY_TOKENS[token.casefold()])
        except KeyError as error:
            raise ScheduleError(f"unknown weekday: {token!r}") from error
    if len(weekdays) == 2:
        if weekdays[0] >= weekdays[1]:
            raise ScheduleError("weekday ranges must move forward and cannot wrap across Sunday")
        return tuple(Weekday(index) for index in range(int(weekdays[0]), int(weekdays[1]) + 1))
    return (weekdays[0],)


def _parse_interval(value: str) -> TimeInterval:
    match = _INTERVAL_PATTERN.fullmatch(value)
    if match is None:
        raise ScheduleError(f"invalid schedule interval: {value!r}")
    start = _parse_time(match.group("start"), allow_24=False)
    end = _parse_time(match.group("end"), allow_24=True)
    if start >= end:
        raise ScheduleError("schedule intervals must have start before end; overnight intervals are not supported")
    return TimeInterval(start, end)


def _parse_time(value: str, *, allow_24: bool) -> int:
    hour_text, minute_text = value.split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    if minute > 59 or hour > 24 or (hour == 24 and (minute != 0 or not allow_24)):
        qualifier = " (24:00 is allowed only as an interval end)" if hour == 24 else ""
        raise ScheduleError(f"invalid 24-hour time: {value}{qualifier}")
    return hour * 60 + minute


def _normalize_intervals(intervals: tuple[TimeInterval, ...]) -> tuple[TimeInterval, ...]:
    if not intervals:
        return ()
    ordered = sorted(intervals)
    normalized: list[TimeInterval] = [ordered[0]]
    for interval in ordered[1:]:
        previous = normalized[-1]
        if interval.start_minute <= previous.end_minute:
            normalized[-1] = TimeInterval(previous.start_minute, max(previous.end_minute, interval.end_minute))
        else:
            normalized.append(interval)
    return tuple(normalized)


def _format_time(minute: int) -> str:
    hours, minutes = divmod(minute, 60)
    return f"{hours:02d}:{minutes:02d}"


__all__ = [
    "DEFAULT_SCHEDULE_TEXT",
    "Schedule",
    "ScheduleError",
    "TimeInterval",
    "classify_marker",
    "default_schedule",
    "parse_schedule",
]
