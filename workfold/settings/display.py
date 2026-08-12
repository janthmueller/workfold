"""Human-readable effective-configuration reporting."""

from __future__ import annotations

from workfold.config import RawOptions
from workfold.sanitization import display_width, pad_right, sanitize_terminal_text
from workfold.settings.model import DEFAULT_SETTINGS, ResolvedSettings, SettingValue


def format_resolved_settings(resolution: ResolvedSettings, options: RawOptions) -> str:
    """Format the effective values and origins for ``--show-config``."""

    lines = ["Configuration files"]
    if resolution.config_disabled:
        lines.append("  disabled by --no-config")
    elif resolution.explicit_config is not None:
        lines.append(f"  Config  {_safe(resolution.explicit_config)}")
    else:
        global_text = (
            _safe(resolution.global_config)
            if resolution.global_config is not None
            else f"{_safe(resolution.global_candidate)} (not found)"
        )
        lines.append(f"  Global  {global_text}")
        lines.append(f"  Local   {_safe(resolution.local_config) if resolution.local_config is not None else '—'}")

    rows = _effective_rows(resolution, options)
    setting_width = max(display_width("Setting"), *(display_width(name) for name, _, _ in rows))
    value_width = max(display_width("Effective value"), *(display_width(value) for _, value, _ in rows))
    lines.extend(("", f"{pad_right('Setting', setting_width)}  {pad_right('Effective value', value_width)}  Origin"))
    for name, value, origin in rows:
        lines.append(f"{pad_right(name, setting_width)}  {pad_right(value, value_width)}  {origin}")
    return "\n".join(lines) + "\n"


def _effective_rows(
    resolution: ResolvedSettings,
    options: RawOptions,
) -> list[tuple[str, str, str]]:
    values = _effective_values(options)
    profile_origin = resolution.origins["profile"]
    profile_controlled = {
        "git-records",
        "git-commit-times",
        "git-commits-from",
        "fs-times",
        "fs-entries",
        "include-ignored",
    }
    rows: list[tuple[str, str, str]] = []
    for key in DEFAULT_SETTINGS:
        origin = resolution.origins[key]
        origin_label = origin.label
        if options.profile.value != "standard" and key in profile_controlled:
            origin_label = f"{profile_origin.label} (profile)"
        rows.append((key, _format_value(values[key]), origin_label))
    return rows


def _effective_values(options: RawOptions) -> dict[str, SettingValue]:
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

    git_records: list[str] = []
    if options.git_records.includes_commits:
        if options.git_mode.includes_commit_markers:
            git_records.append("commit")
        if options.git_mode.includes_file_changes:
            git_records.append("file-change")
    if options.git_records.includes_tags:
        git_records.append("tag")
    if options.git_records.includes_reflogs:
        git_records.append("reflog")

    commit_times = ("author", "committer") if options.git_date.value == "both" else (options.git_date.value,)
    filesystem_time_names = {
        "created": "birth",
        "modified": "modified",
        "changed": "metadata-changed",
        "accessed": "accessed",
    }
    source_mode = options.source.value
    return {
        "time": time_value,
        "mode": source_mode,
        "profile": options.profile.value,
        "git-records": tuple(git_records),
        "git-commit-times": commit_times,
        "git-commits-from": options.ref_scope.value,
        "git-identity": options.git_identities,
        "fs-times": tuple(filesystem_time_names[item.value] for item in options.filesystem_times),
        "fs-entries": tuple(item.value for item in options.filesystem_entries),
        "include-ignored": options.include_ignored,
        "exclude": options.exclusions,
        "hours": options.hours,
        "timezone": options.timezone_name or "local",
        "cluster-window": _format_duration(options.cluster_window.total_seconds()),
        "cluster-anchor": options.cluster_anchor.value,
        "band-label": options.band_label.value,
        "show-empty-bands": options.show_empty_bands,
        "marker-style": options.marker_style.value,
        "grid": options.grid_style.value,
        "display-hours": (
            "auto"
            if options.display_hours is None
            else f"{_format_clock(options.display_hours.start_minute)}-{_format_clock(options.display_hours.end_minute)}"
        ),
        "hide-days": tuple(day.abbreviation for day in options.hide_days),
        "hide-empty-days": tuple(day.abbreviation for day in options.hide_empty_days),
        "no-color": options.no_color,
        "list-outside": options.list_outside,
        "limit": options.limit,
        "coverage": options.coverage,
        "strict": options.strict,
        "verbose": options.verbose,
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
