from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, cast
from zoneinfo import ZoneInfo

import pytest
from workfold.domain.observations import (
    ActivityMarker,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    Weekday,
)
from workfold.domain.schedule import (
    Schedule,
    ScheduleError,
    TimeInterval,
    classify_marker,
    default_schedule,
    parse_schedule,
)
from workfold.domain.time import datetime_to_utc_ns

BERLIN = ZoneInfo("Europe/Berlin")


def _marker(value: datetime) -> ActivityMarker:
    origin = RecordOrigin("commit", Source.GIT, RecordKind.COMMIT, Path("/repo"), commit_id="a" * 40)
    observation = TimestampObservation.create(
        origin,
        TimestampKind.GIT_AUTHOR,
        datetime_to_utc_ns(value),
        value.isoformat(),
        original_offset_minutes=0,
        actor_name="Fixture",
        actor_email="fixture@example.test",
    )
    return ActivityMarker.create((observation,))


def test_default_schedule_has_weekday_hours_and_empty_weekend() -> None:
    schedule = default_schedule()
    assert str(schedule) == "Mo-Fr 08:00-16:30"
    assert schedule.bounds == (480, 990)
    assert schedule.intervals_for(Weekday.MONDAY) == (TimeInterval(480, 990),)
    assert schedule.intervals_for(Weekday.SATURDAY) == ()


@pytest.mark.parametrize("value", ["all", "ALL", "  All  "])
def test_all_schedule_covers_every_minute_of_every_weekday(value: str) -> None:
    schedule = parse_schedule(value)

    assert str(schedule) == "all"
    assert schedule.bounds == (0, 1440)
    assert all(schedule.intervals_for(day) == (TimeInterval(0, 1440),) for day in Weekday)
    assert all(schedule.contains(day, minute) for day in Weekday for minute in (0, 720, 1439))


def test_empty_schedule_has_no_display_bounds() -> None:
    schedule = Schedule(tuple(() for _weekday in Weekday))

    assert schedule.bounds is None


def test_parser_accepts_case_insensitive_days_and_normalizes_interval_unions() -> None:
    schedule = parse_schedule("mo-th 08:00-12:00, 12:00-13:00; MO-TH 11:00-16:30; fr 08:00-14:00; sa 10:00-12:00")
    assert schedule.intervals_for(Weekday.MONDAY) == (TimeInterval(480, 990),)
    assert schedule.intervals_for(Weekday.FRIDAY) == (TimeInterval(480, 840),)
    assert str(schedule) == "Mo-Th 08:00-16:30; Fr 08:00-14:00; Sa 10:00-12:00"


def test_parser_accepts_common_three_letter_weekday_aliases() -> None:
    schedule = parse_schedule("Mo-Thu 08:00-16:30; Fri 08:00-14:00; Sat-Sun 10:00-12:00")

    assert schedule.intervals_for(Weekday.THURSDAY) == (TimeInterval(480, 990),)
    assert schedule.intervals_for(Weekday.FRIDAY) == (TimeInterval(480, 840),)
    assert schedule.intervals_for(Weekday.SUNDAY) == (TimeInterval(600, 720),)
    assert str(schedule) == "Mo-Th 08:00-16:30; Fr 08:00-14:00; Sa-Su 10:00-12:00"


def test_multiple_daily_intervals_preserve_real_breaks() -> None:
    schedule = parse_schedule("Mo-Fr 08:00-12:00,13:00-16:30")
    assert schedule.contains(Weekday.MONDAY, 11 * 60 + 59)
    assert not schedule.contains(Weekday.MONDAY, 12 * 60 + 30)
    assert schedule.contains(Weekday.MONDAY, 13 * 60)


def test_classification_is_start_inclusive_end_exclusive() -> None:
    schedule = parse_schedule("Mo 08:00-16:30")
    assert schedule.contains_local(datetime(2026, 8, 3, 8, 0, tzinfo=BERLIN))
    assert schedule.contains_local(datetime(2026, 8, 3, 16, 29, 59, tzinfo=BERLIN))
    assert not schedule.contains_local(datetime(2026, 8, 3, 16, 30, tzinfo=BERLIN))
    assert not schedule.contains_local(datetime(2026, 8, 4, 8, 0, tzinfo=BERLIN))
    with pytest.raises(ValueError, match="aware"):
        schedule.contains_local(datetime(2026, 8, 3, 8))


def test_2400_is_supported_only_as_an_interval_end() -> None:
    schedule = parse_schedule("Su 00:00-24:00")
    assert schedule.contains(Weekday.SUNDAY, 1439)
    assert str(schedule) == "Su 00:00-24:00"
    with pytest.raises(ScheduleError, match="24:00"):
        parse_schedule("Mo 24:00-24:00")
    with pytest.raises(ScheduleError, match="24-hour"):
        parse_schedule("Mo 23:00-24:01")


@pytest.mark.parametrize(
    "value",
    [
        "",
        ";",
        "Mo-Fr",
        "Mx 08:00-09:00",
        "Fr-Mo 08:00-09:00",
        "Su-Mo 08:00-09:00",
        "Mo 16:30-08:00",
        "Mo 08:00-08:00",
        "Mo 8:00-09:00",
        "Mo 08:60-09:00",
        "Mo 08:00-09:00,",
    ],
)
def test_invalid_schedule_grammar_is_rejected(value: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(value)


def test_day_set_parser_defensively_rejects_more_than_one_range_separator() -> None:
    import workfold.domain.schedule as schedule_module

    parser = cast(Callable[[str], tuple[Weekday, ...]], getattr(schedule_module, "_parse_day_set"))
    with pytest.raises(ScheduleError, match="day set"):
        parser("Mo-Tu-We")


def test_schedule_model_normalizes_direct_construction_and_validates_shape() -> None:
    schedule = Schedule(
        (
            (TimeInterval(600, 700), TimeInterval(500, 600)),
            (),
            (),
            (),
            (),
            (),
            (),
        )
    )
    assert schedule.intervals_for(Weekday.MONDAY) == (TimeInterval(500, 700),)
    with pytest.raises(ScheduleError, match="seven"):
        Schedule(())
    with pytest.raises(ScheduleError, match="start"):
        TimeInterval(60, 60)
    with pytest.raises(ValueError, match="between"):
        TimeInterval(0, 1).contains_minute(1440)


def test_marker_classification_localizes_before_weekday_schedule_without_layout() -> None:
    schedule = parse_schedule("Mo 08:00-09:00")
    marker = _marker(datetime(2026, 8, 3, 8, 29, tzinfo=BERLIN))
    classified = classify_marker(marker, BERLIN, schedule)
    assert classified.weekday is Weekday.MONDAY
    assert classified.minute_of_day == 8 * 60 + 29
    assert classified.within_schedule
    assert not classified.weekend


def test_fall_back_duplicate_wall_times_remain_two_classified_markers() -> None:
    schedule = parse_schedule("Su 02:00-03:00")
    first = classify_marker(_marker(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0)), BERLIN, schedule)
    second = classify_marker(_marker(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)), BERLIN, schedule)
    assert first.marker.occurred_at_utc_ns != second.marker.occurred_at_utc_ns
    assert first.time_of_day_ns == second.time_of_day_ns == 150 * 60 * 1_000_000_000
    assert first.within_schedule and second.within_schedule
    assert first.weekend and second.weekend
