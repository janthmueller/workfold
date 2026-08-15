"""Human-readable effective-configuration reporting."""

from __future__ import annotations

from collections.abc import Sequence

from workfold.configuration.effective import EffectiveSettings
from workfold.configuration.layers import DEFAULT_SETTINGS, ResolvedSettings, SettingValue
from workfold.configuration.options import ListSchedule, RunOptions
from workfold.reporting.sanitization import display_width, pad_right, sanitize_terminal_text
from workfold.reporting.terminal.layout import aligned_fact_lines, column_chunks


def format_resolved_settings(
    resolution: ResolvedSettings,
    effective: EffectiveSettings,
    *,
    width: int = 80,
) -> str:
    """Format the effective values and origins for ``--show-config``."""

    width = max(20, width)
    lines = ["Configuration files"]
    if resolution.config_disabled:
        lines.extend(f"  {chunk}" for chunk in column_chunks("disabled by --no-config", width - 2))
    elif resolution.explicit_config is not None:
        lines.extend(_config_lines((("Config", resolution.explicit_config),), width))
    else:
        global_text = (
            _safe(resolution.global_config)
            if resolution.global_config is not None
            else f"{_safe(resolution.global_candidate)} (not found)"
        )
        lines.extend(
            _config_lines(
                (
                    ("Global", global_text),
                    ("Local", _safe(resolution.local_config) if resolution.local_config is not None else "—"),
                ),
                width,
            )
        )

    rows = _effective_rows(effective)
    lines.extend(("", *_setting_table(rows, width)))
    return "\n".join(lines) + "\n"


def _config_lines(facts: Sequence[tuple[str, object]], width: int) -> list[str]:
    available = max(1, width - 2)
    return [f"  {line}" for line in aligned_fact_lines([(key, _safe(value)) for key, value in facts], available)]


def _setting_table(rows: Sequence[tuple[str, str, str]], width: int) -> list[str]:
    setting_max = max(display_width("Setting"), *(display_width(name) for name, _, _ in rows))
    origin_max = max(display_width("Origin"), *(display_width(origin) for _, _, origin in rows))
    setting_width = min(setting_max, max(7, width // 4))
    remaining = max(2, width - setting_width - 4)
    desired_origin_width = max(8, remaining // 3)
    origin_width = min(origin_max, desired_origin_width, remaining - 1)
    value_width = remaining - origin_width

    table_rows = (("Setting", "Effective value", "Origin"), *rows)
    lines: list[str] = []
    for name, value, origin in table_rows:
        name_chunks = column_chunks(name, setting_width) or [""]
        value_chunks = column_chunks(value, value_width) or [""]
        origin_chunks = column_chunks(origin, origin_width) or [""]
        line_count = max(len(name_chunks), len(value_chunks), len(origin_chunks))
        for index in range(line_count):
            name_part = name_chunks[index] if index < len(name_chunks) else ""
            value_part = value_chunks[index] if index < len(value_chunks) else ""
            origin_part = origin_chunks[index] if index < len(origin_chunks) else ""
            line = f"{pad_right(name_part, setting_width)}  {pad_right(value_part, value_width)}  {origin_part}"
            lines.append(line.rstrip())
    return lines


def _effective_rows(effective: EffectiveSettings) -> list[tuple[str, str, str]]:
    values = _effective_values(effective.options)
    return [(key, _format_value(values[key]), effective.origins[key].label) for key in DEFAULT_SETTINGS]


def _effective_values(options: RunOptions) -> dict[str, SettingValue]:
    preferences = options.terminal
    if options.weeks:
        time_value: SettingValue = options.weeks
    elif options.from_date is not None or options.to_date is not None:
        time_value = f"{options.from_date or ''}..{options.to_date or ''}"
    elif options.rolling_duration is not None:
        time_value = options.rolling_duration.label
    elif options.all_dates:
        time_value = "all"
    else:
        time_value = "this-week"

    event_list = options.terminal.event_list
    if event_list is None:
        list_value: tuple[str, ...] = ()
    elif event_list.schedule is ListSchedule.ALL and not event_list.evidence_kinds:
        list_value = ("all",)
    else:
        list_value = (
            *((event_list.schedule.value,) if event_list.schedule is not ListSchedule.ALL else ()),
            *(kind.value for kind in event_list.evidence_kinds),
        )
    source_mode = options.source.value
    return {
        "time": time_value,
        "mode": source_mode,
        "profile": options.profile.value,
        "events": tuple(kind.value for kind in options.evidence.kinds),
        "git-commits-from": options.ref_scope.value,
        "git-identity": options.git_identities,
        "include-ignored": options.include_ignored,
        "fs-exclude": options.exclusions,
        "hours": options.hours,
        "timezone": options.timezone_name or "local",
        "cluster-window": _format_duration(options.cluster_window.total_seconds()),
        "cluster-anchor": options.cluster_anchor.value,
        "band-label": preferences.band_label.value,
        "show-empty-bands": preferences.show_empty_bands,
        "marker-style": preferences.marker_style.value,
        "grid": preferences.grid_style.value,
        "display-hours": (
            "auto"
            if options.display_hours is None
            else f"{_format_clock(options.display_hours.start_minute)}-{_format_clock(options.display_hours.end_minute)}"
        ),
        "hide-days": tuple(day.abbreviation for day in options.hide_days),
        "hide-empty-days": tuple(day.abbreviation for day in options.hide_empty_days),
        "no-color": preferences.no_color,
        "list": list_value,
        "limit": preferences.event_limit,
        "coverage": preferences.coverage,
        "strict": preferences.strict,
        "verbose": preferences.verbose,
    }


def _format_value(value: SettingValue) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(_safe(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return _safe(value)


def _format_clock(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def _format_duration(seconds_value: float) -> str:
    seconds = int(seconds_value)
    parts: list[str] = []
    for suffix, size in (("h", 3_600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{suffix}")
    return "".join(parts)


def _safe(value: object) -> str:
    return sanitize_terminal_text(value)


__all__ = ["format_resolved_settings"]
