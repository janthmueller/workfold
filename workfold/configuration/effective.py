"""Materialize merged settings as validated invocation options."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from workfold.configuration.layers import DEFAULT_SETTINGS, OriginKind, ResolvedSettings, SettingValue
from workfold.configuration.options import RunOptions, UnresolvedOptions
from workfold.configuration.resolution import resolve_options
from workfold.configuration.validation import validate_setting_values


def options_from_settings(resolution: ResolvedSettings, paths: Sequence[Path]) -> RunOptions:
    """Materialize merged settings once through the existing domain validator."""

    validate_setting_values(resolution)
    values = dict(resolution.values)
    explicit_keys = {key for key, origin in resolution.origins.items() if origin.kind is not OriginKind.BUILTIN}
    _suppress_shadowed_settings(values, explicit_keys, resolution)
    unresolved = _unresolved_options(values, explicit_keys, paths)
    return resolve_options(unresolved)


def _unresolved_options(
    values: Mapping[str, SettingValue],
    explicit_keys: set[str],
    paths: Sequence[Path],
) -> UnresolvedOptions:
    key_to_destination = {
        "time": "time_selectors",
        "mode": "modes",
        "profile": "profiles",
        "git-records": "git_records",
        "git-commit-times": "commit_times",
        "git-commits-from": "commits_from",
        "git-identity": "git_identities",
        "fs-times": "filesystem_times",
        "fs-entries": "filesystem_entries",
        "include-ignored": "include_ignored",
        "exclude": "exclusions",
        "hours": "hours",
        "timezone": "timezone_name",
        "cluster-window": "cluster_window",
        "cluster-anchor": "cluster_anchor",
        "band-label": "band_label",
        "show-empty-bands": "show_empty_bands",
        "marker-style": "marker_style",
        "grid": "grid_style",
        "display-hours": "display_hours",
        "hide-days": "hide_days",
        "hide-empty-days": "hide_empty_days",
        "no-color": "no_color",
        "list-outside": "list_outside",
        "limit": "limit",
        "coverage": "coverage",
        "strict": "strict",
        "verbose": "verbose",
    }
    explicit_destinations = {key_to_destination[key] for key in explicit_keys}
    return UnresolvedOptions(
        paths=tuple(paths),
        time_selectors=_tuple_value(values, "time"),
        modes=_tuple_value(values, "mode"),
        profiles=_tuple_value(values, "profile"),
        git_records=_csv_value(values, "git-records"),
        commit_times=_csv_value(values, "git-commit-times"),
        commits_from=_optional_string(values, "git-commits-from"),
        git_identities=_tuple_value(values, "git-identity"),
        filesystem_times=_csv_value(values, "fs-times"),
        filesystem_entries=_csv_value(values, "fs-entries"),
        include_ignored=_boolean_value(values, "include-ignored"),
        exclusions=_tuple_value(values, "exclude"),
        hours=_string_value(values, "hours"),
        timezone_name=_optional_sentinel(values, "timezone", sentinel="local"),
        cluster_window=_string_value(values, "cluster-window"),
        cluster_anchor=_string_value(values, "cluster-anchor"),
        band_label=_string_value(values, "band-label"),
        show_empty_bands=_boolean_value(values, "show-empty-bands"),
        marker_style=_string_value(values, "marker-style"),
        grid_style=_string_value(values, "grid"),
        display_hours=_optional_sentinel(values, "display-hours", sentinel="auto"),
        hide_days=_tuple_value(values, "hide-days"),
        hide_empty_days=_tuple_value(values, "hide-empty-days"),
        no_color=_boolean_value(values, "no-color"),
        list_outside=_boolean_value(values, "list-outside"),
        limit=_integer_value(values, "limit"),
        coverage=_boolean_value(values, "coverage"),
        strict=_boolean_value(values, "strict"),
        verbose=_boolean_value(values, "verbose"),
        explicit_names=frozenset(explicit_destinations),
    )


def _suppress_shadowed_settings(
    values: dict[str, SettingValue],
    explicit_keys: set[str],
    resolution: ResolvedSettings,
) -> None:
    profiles = _tuple_value(values, "profile")
    if len(profiles) == 1 and profiles[0] in {"portable", "full"}:
        profile_precedence = resolution.origins["profile"].precedence
        _reset_lower_precedence(
            values,
            explicit_keys,
            resolution,
            precedence=profile_precedence,
            keys=(
                "git-records",
                "git-commit-times",
                "git-commits-from",
                "fs-times",
                "fs-entries",
                "include-ignored",
            ),
        )

    modes = _tuple_value(values, "mode")
    if len(modes) == 1:
        irrelevant = {
            "git": ("fs-times", "fs-entries", "include-ignored", "exclude"),
            "fs": ("git-records", "git-commit-times", "git-commits-from", "git-identity"),
            "both": (),
        }[modes[0]]
        _reset_lower_precedence(
            values,
            explicit_keys,
            resolution,
            precedence=resolution.origins["mode"].precedence,
            keys=irrelevant,
        )

    if not _boolean_value(values, "list-outside"):
        _discard_lower_precedence(
            explicit_keys,
            resolution,
            precedence=resolution.origins["list-outside"].precedence,
            keys=("limit",),
        )


def _reset_lower_precedence(
    values: dict[str, SettingValue],
    explicit_keys: set[str],
    resolution: ResolvedSettings,
    *,
    precedence: int,
    keys: Sequence[str],
) -> None:
    for key in keys:
        if resolution.origins[key].precedence < precedence:
            values[key] = DEFAULT_SETTINGS[key]
            explicit_keys.discard(key)


def _discard_lower_precedence(
    explicit_keys: set[str],
    resolution: ResolvedSettings,
    *,
    precedence: int,
    keys: Sequence[str],
) -> None:
    for key in keys:
        if resolution.origins[key].precedence < precedence:
            explicit_keys.discard(key)


def _tuple_value(values: Mapping[str, SettingValue], key: str) -> tuple[str, ...]:
    value = values[key]
    if not isinstance(value, tuple):
        raise AssertionError(f"internal setting {key!r} is not a tuple")
    return value


def _csv_value(values: Mapping[str, SettingValue], key: str) -> str | None:
    value = values[key]
    if value is None:
        return None
    if not isinstance(value, tuple):
        raise AssertionError(f"internal setting {key!r} is not a tuple")
    return ",".join(value)


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


def _optional_sentinel(values: Mapping[str, SettingValue], key: str, *, sentinel: str) -> str | None:
    value = _optional_string(values, key)
    return None if value == sentinel else value


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


__all__ = ["options_from_settings"]
