"""Operating-system adapter for resolving the local IANA timezone."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from workfold.domain.time import TimeRangeError, resolve_timezone


class LocalTimezoneResolutionError(TimeRangeError):
    """Raised when the OS local timezone cannot be resolved to an IANA zone."""


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


def _tzlocal_zone_name() -> str | None:
    try:
        return get_localzone_name()
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return None


__all__ = ["LocalTimezoneResolutionError", "resolve_local_timezone"]
