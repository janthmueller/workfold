from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from workfold.domain.time import (
    InstantRange,
    InstantRangeUnion,
    TimeRangeError,
    all_time_range,
    calendar_date_range,
    current_week_range,
    datetime_to_utc_ns,
    iso_week_range,
    iso_week_union,
    parse_calendar_date,
    parse_iso_week,
    resolve_timezone,
    rolling_duration_range,
    utc_ns_to_datetime,
)

UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")


def test_integer_nanoseconds_avoid_float_rounding_and_handle_pre_epoch() -> None:
    value = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert datetime_to_utc_ns(value) == -1_000
    assert utc_ns_to_datetime(-1, UTC) == value
    with pytest.raises(TimeRangeError, match="timezone-aware"):
        datetime_to_utc_ns(datetime(2026, 1, 1))


def test_half_open_range_membership_and_validation() -> None:
    selected = InstantRange(10, 20)
    assert selected.contains(10)
    assert selected.contains(19)
    assert not selected.contains(20)
    with pytest.raises(TimeRangeError, match="start before end"):
        InstantRange(10, 10)


def test_range_union_normalizes_duplicates_adjacent_and_disjoint_ranges() -> None:
    selected = InstantRangeUnion(
        (
            InstantRange(30, 40),
            InstantRange(10, 20),
            InstantRange(20, 30),
            InstantRange(60, 70),
            InstantRange(10, 20),
        )
    )
    assert selected.ranges == (InstantRange(10, 40), InstantRange(60, 70))
    assert 10 in selected
    assert 50 not in selected
    assert "10" not in selected
    assert not selected.is_empty
    assert not selected.is_unbounded


def test_unbounded_and_empty_unions() -> None:
    assert InstantRangeUnion(()).is_empty
    assert all_time_range().is_unbounded
    assert all_time_range().contains(-(10**30))
    assert InstantRangeUnion((InstantRange(None, 10), InstantRange(10, 20))).ranges == (InstantRange(None, 20),)
    assert InstantRangeUnion((InstantRange(10, 20), InstantRange(20, None))).is_unbounded is False
    assert InstantRangeUnion((InstantRange(None, 0), InstantRange(0, None))).is_unbounded


def test_iso_week_validation_and_repeated_union() -> None:
    assert parse_iso_week("2026-W31") == (2026, 31)
    assert iso_week_range("2026-W31", UTC).start_utc_ns == datetime_to_utc_ns(datetime(2026, 7, 27, tzinfo=UTC))
    selected = iso_week_union(("2026-W31", "2026-W31", "2026-W33"), UTC)
    assert len(selected.ranges) == 2
    with pytest.raises(TimeRangeError, match="expected"):
        parse_iso_week("2026-31")
    with pytest.raises(TimeRangeError, match="invalid ISO week"):
        parse_iso_week("2026-W54")


def test_ranges_reject_unrepresentable_exclusive_calendar_boundaries() -> None:
    with pytest.raises(TimeRangeError, match="exclusive end"):
        iso_week_range("9999-W52", UTC)
    with pytest.raises(TimeRangeError, match="exclusive end"):
        calendar_date_range(None, date.max, UTC)
    with pytest.raises(TimeRangeError, match="exclusive end"):
        current_week_range(datetime.max.replace(tzinfo=UTC), UTC)
    with pytest.raises(TimeRangeError, match="at least one"):
        iso_week_union((), UTC)


@pytest.mark.parametrize(
    ("value", "zone"),
    [
        (datetime.min, ZoneInfo("Asia/Kolkata")),
        (datetime.max, ZoneInfo("America/New_York")),
    ],
)
def test_calendar_boundaries_reject_timezone_conversion_overflow(
    value: datetime,
    zone: ZoneInfo,
) -> None:
    with pytest.raises(TimeRangeError, match="representable UTC"):
        datetime_to_utc_ns(value.replace(tzinfo=zone))


def test_inclusive_calendar_date_range_and_open_endpoints() -> None:
    selected = calendar_date_range(date(2026, 7, 1), date(2026, 7, 31), BERLIN)
    assert selected.contains(datetime_to_utc_ns(datetime(2026, 7, 31, 23, 59, tzinfo=BERLIN)))
    assert not selected.contains(datetime_to_utc_ns(datetime(2026, 8, 1, tzinfo=BERLIN)))
    assert calendar_date_range(date(2026, 1, 1), None, UTC).ranges[0].end_utc_ns is None
    assert calendar_date_range(None, date(2026, 1, 1), UTC).ranges[0].start_utc_ns is None
    with pytest.raises(TimeRangeError, match="must not be after"):
        calendar_date_range(date(2026, 2, 1), date(2026, 1, 1), UTC)


def test_calendar_boundaries_follow_dst_instead_of_assuming_24_hours() -> None:
    spring = calendar_date_range(date(2026, 3, 29), date(2026, 3, 29), BERLIN).ranges[0]
    fall = calendar_date_range(date(2026, 10, 25), date(2026, 10, 25), BERLIN).ranges[0]
    assert spring.end_utc_ns is not None and spring.start_utc_ns is not None
    assert fall.end_utc_ns is not None and fall.start_utc_ns is not None
    assert spring.end_utc_ns - spring.start_utc_ns == 23 * 60 * 60 * 1_000_000_000
    assert fall.end_utc_ns - fall.start_utc_ns == 25 * 60 * 60 * 1_000_000_000


def test_calendar_range_for_a_skipped_local_date_is_empty() -> None:
    selected = calendar_date_range(
        date(2011, 12, 30),
        date(2011, 12, 30),
        ZoneInfo("Pacific/Apia"),
    )

    assert selected.is_empty


def test_fall_back_instants_remain_distinct_in_the_same_wall_time() -> None:
    first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
    first_ns = datetime_to_utc_ns(first)
    second_ns = datetime_to_utc_ns(second)
    assert second_ns - first_ns == 3_600_000_000_000
    assert utc_ns_to_datetime(first_ns, BERLIN).hour == utc_ns_to_datetime(second_ns, BERLIN).hour == 2


def test_current_week_uses_selected_timezone() -> None:
    now = datetime(2026, 8, 3, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
    selected = current_week_range(now, BERLIN)
    expected_monday = datetime(2026, 7, 27, tzinfo=BERLIN)
    assert selected.ranges[0].start_utc_ns == datetime_to_utc_ns(expected_monday)
    with pytest.raises(TimeRangeError, match="timezone-aware"):
        current_week_range(datetime(2026, 8, 3), BERLIN)


def test_rolling_duration_is_half_open_and_uses_elapsed_time_across_dst() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=BERLIN)
    selected = rolling_duration_range(now, timedelta(days=2))
    start = datetime(2026, 3, 28, 11, 0, tzinfo=BERLIN)

    assert selected.ranges == (InstantRange(datetime_to_utc_ns(start), datetime_to_utc_ns(now)),)
    assert datetime_to_utc_ns(start) in selected
    assert datetime_to_utc_ns(now) not in selected


def test_rolling_duration_rejects_invalid_clock_and_duration() -> None:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    with pytest.raises(TimeRangeError, match="timezone-aware"):
        rolling_duration_range(datetime(2026, 8, 11), timedelta(days=1))
    with pytest.raises(TimeRangeError, match="positive"):
        rolling_duration_range(now, timedelta(0))


def test_timezone_and_calendar_parsers_are_actionable() -> None:
    assert resolve_timezone(" Europe/Berlin ").key == "Europe/Berlin"
    assert parse_calendar_date("2026-08-09") == date(2026, 8, 9)
    with pytest.raises(TimeRangeError, match="unknown IANA"):
        resolve_timezone("Mars/Olympus")
    with pytest.raises(TimeRangeError, match="must not be empty"):
        resolve_timezone("  ")
    with pytest.raises(TimeRangeError, match="calendar date"):
        parse_calendar_date("09.08.2026")
    with pytest.raises(TimeRangeError, match="calendar date"):
        parse_calendar_date("20260809")
