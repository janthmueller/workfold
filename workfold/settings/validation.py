"""Domain-syntax validation with configuration-file attribution."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from workfold.config import (
    UsageError,
    parse_cluster_window,
    parse_commit_times,
    parse_display_hours,
    parse_filesystem_entries,
    parse_filesystem_times,
    parse_git_records,
    parse_time_selectors,
    parse_weekday_scopes,
)
from workfold.models import Weekday
from workfold.schedule import parse_schedule
from workfold.settings.model import OriginKind, ResolvedSettings, SettingValue
from workfold.time_bands import ClusterAnchor, validate_cluster_window_alignment
from workfold.time_ranges import resolve_timezone


def validate_setting_values(resolution: ResolvedSettings) -> None:
    """Validate each effective value and attribute file-backed failures."""

    values = resolution.values
    _validate(resolution, "time", lambda: parse_time_selectors(_tuple_value(values, "time")))
    _validate(resolution, "git-records", lambda: parse_git_records(_csv_value(values, "git-records")))
    _validate(
        resolution,
        "git-commit-times",
        lambda: parse_commit_times(_csv_value(values, "git-commit-times")),
    )
    _validate(resolution, "git-identity", lambda: _validate_nonempty(_tuple_value(values, "git-identity")))
    _validate(resolution, "fs-times", lambda: parse_filesystem_times(_csv_value(values, "fs-times")))
    _validate(resolution, "fs-entries", lambda: parse_filesystem_entries(_csv_value(values, "fs-entries")))
    _validate(resolution, "exclude", lambda: _validate_exclusions(_tuple_value(values, "exclude")))
    _validate(resolution, "hours", lambda: parse_schedule(_string_value(values, "hours")))
    _validate(resolution, "timezone", lambda: _validate_timezone(_optional_string(values, "timezone")))
    _validate(
        resolution,
        "cluster-window",
        lambda: parse_cluster_window(_string_value(values, "cluster-window")),
    )
    _validate_cluster_settings(resolution)
    _validate(
        resolution,
        "display-hours",
        lambda: _validate_display_hours(_optional_string(values, "display-hours")),
    )
    _validate(
        resolution,
        "hide-days",
        lambda: _validate_hidden_days(_tuple_value(values, "hide-days")),
    )
    _validate(
        resolution,
        "hide-empty-days",
        lambda: parse_weekday_scopes(_tuple_value(values, "hide-empty-days"), option="--hide-empty-days"),
    )
    _validate(resolution, "limit", lambda: _validate_limit(_integer_value(values, "limit")))


def _validate(resolution: ResolvedSettings, key: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except ValueError as error:
        origin = resolution.origins[key]
        if origin.kind in {OriginKind.GLOBAL, OriginKind.LOCAL, OriginKind.EXPLICIT} and origin.path is not None:
            raise UsageError(f"{origin.path}: {key}: {error}") from error
        if isinstance(error, UsageError):
            raise
        raise UsageError(str(error)) from error


def _validate_cluster_settings(resolution: ResolvedSettings) -> None:
    window_text = _string_value(resolution.values, "cluster-window")
    anchor_text = _string_value(resolution.values, "cluster-anchor")
    cluster_window = parse_cluster_window(window_text)
    cluster_anchor = ClusterAnchor(anchor_text)
    show_empty_bands = _boolean_value(resolution.values, "show-empty-bands")

    try:
        validate_cluster_window_alignment(cluster_window, cluster_anchor)
    except ValueError as error:
        raise UsageError(
            "--cluster-anchor midnight requires --cluster-window to use whole minutes "
            "so fixed HH:MM band labels remain exact; effective settings: "
            f"cluster-anchor={anchor_text} ({_format_origin(resolution, 'cluster-anchor')}); "
            f"cluster-window={window_text} ({_format_origin(resolution, 'cluster-window')})"
        ) from error

    if show_empty_bands and cluster_anchor is not ClusterAnchor.MIDNIGHT:
        raise UsageError(
            "--show-empty-bands requires --cluster-anchor midnight; effective settings: "
            f"show-empty-bands=true ({_format_origin(resolution, 'show-empty-bands')}); "
            f"cluster-anchor={anchor_text} ({_format_origin(resolution, 'cluster-anchor')})"
        )


def _format_origin(resolution: ResolvedSettings, key: str) -> str:
    origin = resolution.origins[key]
    if origin.path is not None:
        return f"{origin.label} {origin.path}"
    return origin.label


def _validate_nonempty(values: tuple[str, ...]) -> None:
    if any(not value.strip() for value in values):
        raise UsageError("--git-identity values cannot be empty")


def _validate_exclusions(values: tuple[str, ...]) -> None:
    if any(not value.strip() for value in values):
        raise UsageError("--exclude values cannot be empty")
    if any(value.strip().startswith("!") for value in values):
        raise UsageError("--exclude patterns cannot be negated; explicit exclusions always win")


def _validate_timezone(value: str | None) -> None:
    if value is not None and value != "local":
        resolve_timezone(value)


def _validate_display_hours(value: str | None) -> None:
    if value is not None and value != "auto":
        parse_display_hours(value)


def _validate_hidden_days(values: tuple[str, ...]) -> None:
    hidden = parse_weekday_scopes(values, option="--hide-days")
    if hidden == tuple(Weekday):
        raise UsageError("--hide-days cannot hide all seven weekday columns")


def _validate_limit(value: int) -> None:
    if value < 1:
        raise UsageError("--limit must be at least 1")


def _tuple_value(values: Mapping[str, SettingValue], key: str) -> tuple[str, ...]:
    value = values[key]
    if not isinstance(value, tuple):
        raise AssertionError(f"internal setting {key!r} is not a tuple")
    return value


def _csv_value(values: Mapping[str, SettingValue], key: str) -> str:
    return ",".join(_tuple_value(values, key))


def _string_value(values: Mapping[str, SettingValue], key: str) -> str:
    value = values[key]
    if not isinstance(value, str):
        raise AssertionError(f"internal setting {key!r} is not a string")
    return value


def _optional_string(values: Mapping[str, SettingValue], key: str) -> str | None:
    value = values[key]
    if value is not None and not isinstance(value, str):
        raise AssertionError(f"internal setting {key!r} is not an optional string")
    return value


def _boolean_value(values: Mapping[str, SettingValue], key: str) -> bool:
    value = values[key]
    if not isinstance(value, bool):
        raise AssertionError(f"internal setting {key!r} is not a boolean")
    return value


def _integer_value(values: Mapping[str, SettingValue], key: str) -> int:
    value = values[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError(f"internal setting {key!r} is not an integer")
    return value


__all__ = ["validate_setting_values"]
