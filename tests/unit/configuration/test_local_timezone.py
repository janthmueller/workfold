from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workfold.configuration.local_timezone import LocalTimezoneResolutionError, resolve_local_timezone


def test_local_timezone_resolver_accepts_injected_named_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import workfold.configuration.local_timezone as local_timezone

    missing = tmp_path / "missing"
    assert (
        resolve_local_timezone(environ={"TZ": "Europe/Berlin"}, timezone_file=missing, localtime_file=missing).key
        == "Europe/Berlin"
    )
    monkeypatch.setattr(local_timezone, "_tzlocal_zone_name", lambda: None)
    timezone_file = tmp_path / "timezone"
    timezone_file.write_text("Europe/Paris\n", encoding="utf-8")
    assert resolve_local_timezone(environ={}, timezone_file=timezone_file, localtime_file=missing).key == "Europe/Paris"


def test_local_timezone_resolver_reads_zoneinfo_symlink_and_skips_bad_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import workfold.configuration.local_timezone as local_timezone

    monkeypatch.setattr(local_timezone, "_tzlocal_zone_name", lambda: None)
    localtime = tmp_path / "localtime"
    localtime.symlink_to("/usr/share/zoneinfo/Europe/Paris")
    zone = resolve_local_timezone(
        environ={"TZ": "Not/AZone"},
        timezone_file=tmp_path / "missing",
        localtime_file=localtime,
    )
    assert zone.key == "Europe/Paris"


def test_local_timezone_resolver_can_use_zoneinfo_from_current_datetime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import workfold.configuration.local_timezone as local_timezone

    monkeypatch.setattr(local_timezone, "_tzlocal_zone_name", lambda: None)
    monkeypatch.setattr(local_timezone, "datetime", _ZoneInfoNow)
    missing = tmp_path / "missing"
    assert resolve_local_timezone(environ={}, timezone_file=missing, localtime_file=missing).key == "Europe/Berlin"


def test_tzlocal_adapter_failure_is_accounted(monkeypatch: pytest.MonkeyPatch) -> None:
    import workfold.configuration.local_timezone as local_timezone

    def fail_resolution() -> str:
        raise OSError

    monkeypatch.setattr(local_timezone, "get_localzone_name", fail_resolution)
    adapter = getattr(local_timezone, "_tzlocal_zone_name")
    assert adapter() is None


def test_local_timezone_resolver_rejects_fixed_offset_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import workfold.configuration.local_timezone as local_timezone

    monkeypatch.setattr(local_timezone, "_tzlocal_zone_name", lambda: None)
    monkeypatch.setattr(local_timezone, "datetime", _FixedOffsetNow)
    missing = tmp_path / "missing"
    with pytest.raises(LocalTimezoneResolutionError, match="DST-capable"):
        resolve_local_timezone(environ={}, timezone_file=missing, localtime_file=missing)


class _FixedOffsetNow:
    @staticmethod
    def now() -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))


class _ZoneInfoNow:
    @staticmethod
    def now() -> _ZoneInfoValue:
        return _ZoneInfoValue()


class _ZoneInfoValue:
    tzinfo = ZoneInfo("Europe/Berlin")

    def astimezone(self) -> _ZoneInfoValue:
        return self
