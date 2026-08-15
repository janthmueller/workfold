"""Materialize merged settings as validated invocation options."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from workfold.configuration.layers import (
    DEFAULT_SETTINGS,
    OriginKind,
    ResolvedSettings,
    SettingOrigin,
    SettingValue,
)
from workfold.configuration.options import RunOptions, UnresolvedOptions, UsageError
from workfold.configuration.parsing import parse_event_selectors
from workfold.configuration.resolution import resolve_options
from workfold.configuration.validation import validate_setting_values
from workfold.domain.observations import RecordKind, Source


@dataclass(frozen=True, slots=True)
class EffectiveOrigin:
    """Structured provenance for one materialized setting."""

    dependencies: tuple[tuple[str, SettingOrigin], ...]
    explanation: str | None = None

    @classmethod
    def direct(cls, origin: SettingOrigin) -> EffectiveOrigin:
        """Describe a value supplied directly by one precedence layer."""

        return cls((("", origin),))

    @property
    def label(self) -> str:
        """Return a compact human-readable provenance label."""

        first_origin = self.dependencies[0][1]
        if all(origin == first_origin for _role, origin in self.dependencies):
            base = first_origin.label
        else:
            base = " + ".join(f"{origin.label} {role}" for role, origin in self.dependencies)
        if self.explanation is not None:
            return f"{base} ({self.explanation})"
        return base


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    """Materialized options together with shadow-adjusted values and origins."""

    options: RunOptions
    values: Mapping[str, SettingValue]
    origins: Mapping[str, EffectiveOrigin]


def materialize_settings(resolution: ResolvedSettings, paths: Sequence[Path]) -> EffectiveSettings:
    """Materialize merged settings and retain the provenance of the result."""

    try:
        validate_setting_values(resolution)
        values = dict(resolution.values)
        origins = {key: EffectiveOrigin.direct(origin) for key, origin in resolution.origins.items()}
        explicit_keys = {key for key, origin in resolution.origins.items() if origin.kind is not OriginKind.BUILTIN}
        _suppress_shadowed_settings(values, origins, explicit_keys, resolution)
        unresolved = _unresolved_options(values, explicit_keys, paths)
        options = resolve_options(unresolved)
    except UsageError as error:
        _raise_with_setting_origins(error, resolution)
    return EffectiveSettings(
        options=options,
        values=values,
        origins=origins,
    )


def _raise_with_setting_origins(error: UsageError, resolution: ResolvedSettings) -> NoReturn:
    """Re-raise a structured cross-setting error with every relevant origin."""

    keyed_origins = tuple((key, resolution.origins[key]) for key in error.setting_keys if key in resolution.origins)
    file_backed = tuple(
        (key, origin)
        for key, origin in keyed_origins
        if origin.kind in {OriginKind.GLOBAL, OriginKind.LOCAL, OriginKind.EXPLICIT} and origin.path is not None
    )
    if file_backed:
        file_paths = {origin.path for _key, origin in file_backed}
        if len(file_backed) == len(keyed_origins) and len(file_paths) == 1:
            path = next(iter(file_paths))
            keys = ", ".join(key for key, _origin in keyed_origins)
            message = f"{path}: {keys}: {error}"
        else:
            details = "; ".join(f"{key} ({_format_setting_origin(origin)})" for key, origin in keyed_origins)
            message = f"{error}; setting origins: {details}"
        raise UsageError(message, setting_keys=error.setting_keys) from error
    raise error


def _format_setting_origin(origin: SettingOrigin) -> str:
    if origin.path is not None:
        return f"{origin.label} {origin.path}"
    return origin.label


def options_from_settings(resolution: ResolvedSettings, paths: Sequence[Path]) -> RunOptions:
    """Materialize merged settings once through the existing domain validator."""

    return materialize_settings(resolution, paths).options


def _unresolved_options(
    values: Mapping[str, SettingValue],
    explicit_keys: set[str],
    paths: Sequence[Path],
) -> UnresolvedOptions:
    key_to_destination = {
        "time": "time_selectors",
        "mode": "modes",
        "profile": "profiles",
        "events": "event_selectors",
        "git-commits-from": "commits_from",
        "git-identity": "git_identities",
        "include-ignored": "include_ignored",
        "fs-exclude": "exclusions",
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
        "list": "list_selectors",
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
        event_selectors=_optional_tuple(values, "events"),
        commits_from=_optional_string(values, "git-commits-from"),
        git_identities=_tuple_value(values, "git-identity"),
        include_ignored=_boolean_value(values, "include-ignored"),
        exclusions=_tuple_value(values, "fs-exclude"),
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
        list_selectors=_tuple_value(values, "list") if _tuple_value(values, "list") else None,
        limit=_integer_value(values, "limit"),
        coverage=_boolean_value(values, "coverage"),
        strict=_boolean_value(values, "strict"),
        verbose=_boolean_value(values, "verbose"),
        explicit_names=frozenset(explicit_destinations),
    )


def _suppress_shadowed_settings(
    values: dict[str, SettingValue],
    origins: dict[str, EffectiveOrigin],
    explicit_keys: set[str],
    resolution: ResolvedSettings,
) -> None:
    events = _optional_tuple(values, "events")
    event_precedence = resolution.origins["events"].precedence if events is not None else -1
    preset_precedence = max(resolution.origins["mode"].precedence, resolution.origins["profile"].precedence)
    if event_precedence == preset_precedence and event_precedence > 0:
        conflicting_presets = tuple(
            key for key in ("mode", "profile") if resolution.origins[key].precedence == event_precedence
        )
        raise UsageError(
            "--events cannot be combined with --mode or --profile at the same precedence layer",
            setting_keys=("events", *conflicting_presets),
        )
    if event_precedence > preset_precedence:
        # Custom event selection is a complete alternative to a preset. Do
        # not inherit scope broadening that a lower-precedence profile would
        # otherwise apply during expansion.
        values["mode"] = DEFAULT_SETTINGS["mode"]
        values["profile"] = DEFAULT_SETTINGS["profile"]
        explicit_keys.discard("mode")
        explicit_keys.discard("profile")
        event_origin = resolution.origins["events"]
        derived_from_events = EffectiveOrigin((("events", event_origin),), "derived from events")
        origins["mode"] = derived_from_events
        origins["profile"] = derived_from_events
    elif preset_precedence > event_precedence and events is not None:
        values["events"] = None
        explicit_keys.discard("events")

    preset_origin = EffectiveOrigin(
        (
            ("mode", resolution.origins["mode"]),
            ("profile", resolution.origins["profile"]),
        ),
        "preset expansion",
    )

    profiles = _tuple_value(values, "profile")
    if len(profiles) == 1 and profiles[0] in {"portable", "full"}:
        profile_precedence = resolution.origins["profile"].precedence
        _reset_lower_precedence(
            values,
            origins,
            explicit_keys,
            resolution,
            precedence=profile_precedence,
            effective_origin=EffectiveOrigin((("profile", resolution.origins["profile"]),), "profile"),
            keys=(
                "git-commits-from",
                "include-ignored",
            ),
        )

    active_events = _optional_tuple(values, "events")
    if active_events is not None:
        event_selection = parse_event_selectors(active_events)
        selected_sources = event_selection.sources
        source_key = "both" if len(selected_sources) == 2 else ("git" if selected_sources == (Source.GIT,) else "fs")
        source_precedence = resolution.origins["events"].precedence
        source_origin = EffectiveOrigin((("events", resolution.origins["events"]),), "event selection")
        has_commit_derived_events = any(
            kind.record_kind in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE} for kind in event_selection.kinds
        )
        if not has_commit_derived_events:
            _reset_lower_precedence(
                values,
                origins,
                explicit_keys,
                resolution,
                precedence=source_precedence,
                effective_origin=source_origin,
                keys=("git-commits-from",),
            )
    else:
        modes = _tuple_value(values, "mode")
        source_key = modes[0] if len(modes) == 1 else "git"
        source_precedence = preset_precedence
        source_origin = EffectiveOrigin(preset_origin.dependencies, "source selection")
        origins["events"] = preset_origin
    irrelevant = {
        "git": ("include-ignored", "fs-exclude"),
        "fs": ("git-commits-from", "git-identity"),
        "both": (),
    }[source_key]
    if irrelevant:
        _reset_lower_precedence(
            values,
            origins,
            explicit_keys,
            resolution,
            precedence=source_precedence,
            effective_origin=source_origin,
            keys=irrelevant,
        )

    list_values = _tuple_value(values, "list")
    if not list_values or tuple(value.casefold() for value in list_values) == ("none",):
        _discard_lower_precedence(
            explicit_keys,
            resolution,
            precedence=resolution.origins["list"].precedence,
            keys=("limit",),
        )


def _reset_lower_precedence(
    values: dict[str, SettingValue],
    origins: dict[str, EffectiveOrigin],
    explicit_keys: set[str],
    resolution: ResolvedSettings,
    *,
    precedence: int,
    effective_origin: EffectiveOrigin,
    keys: Sequence[str],
) -> None:
    for key in keys:
        if resolution.origins[key].precedence < precedence:
            values[key] = DEFAULT_SETTINGS[key]
            origins[key] = effective_origin
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


def _optional_tuple(values: Mapping[str, SettingValue], key: str) -> tuple[str, ...] | None:
    value = values[key]
    if value is None:
        return None
    if not isinstance(value, tuple):
        raise AssertionError(f"internal setting {key!r} is not an optional tuple")
    return value


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


__all__ = [
    "EffectiveOrigin",
    "EffectiveSettings",
    "materialize_settings",
    "options_from_settings",
]
