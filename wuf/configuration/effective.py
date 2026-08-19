"""Materialize merged settings as validated invocation options."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from wuf.configuration.layers import (
    OriginKind,
    ResolvedSettings,
    SettingOrigin,
)
from wuf.configuration.options import RunOptions, UnresolvedOptions, UsageError
from wuf.configuration.parsing import parse_event_selectors
from wuf.configuration.profiles import EventProfile, evidence_for_profile
from wuf.configuration.resolution import resolve_options
from wuf.configuration.schema import DEFAULT_SETTINGS, SETTING_BY_KEY, SettingValue
from wuf.configuration.styles import compile_event_style_sheet
from wuf.domain.observations import RecordKind, Source


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
        values = dict(resolution.values)
        origins = {key: EffectiveOrigin.direct(origin) for key, origin in resolution.origins.items()}
        explicit_keys = {key for key, origin in resolution.origins.items() if origin.kind is not OriginKind.BUILTIN}
        _suppress_shadowed_settings(values, origins, explicit_keys, resolution)
        unresolved = _unresolved_options(values, explicit_keys, paths)
        options = resolve_options(unresolved)
        event_styles = compile_event_style_sheet(tuple(layer.rules for layer in resolution.style_layers))
        options = replace(options, terminal=replace(options.terminal, event_styles=event_styles))
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
            details = "; ".join(
                (
                    f"{key}={_format_setting_value(resolution.values[key])} ({_format_setting_origin(origin)})"
                    if error.include_setting_values
                    else f"{key} ({_format_setting_origin(origin)})"
                )
                for key, origin in keyed_origins
            )
            message = f"{error}; setting origins: {details}"
        raise UsageError(message, setting_keys=error.setting_keys) from error
    raise error


def _format_setting_origin(origin: SettingOrigin) -> str:
    if origin.path is not None:
        return f"{origin.label} {origin.path}"
    return origin.label


def _format_setting_value(value: SettingValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return "[" + ", ".join(value) + "]"
    return "none" if value is None else str(value)


def options_from_settings(resolution: ResolvedSettings, paths: Sequence[Path]) -> RunOptions:
    """Materialize merged settings once through the existing domain validator."""

    return materialize_settings(resolution, paths).options


def _unresolved_options(
    values: Mapping[str, SettingValue],
    explicit_keys: set[str],
    paths: Sequence[Path],
) -> UnresolvedOptions:
    explicit_destinations = {SETTING_BY_KEY[key].cli_destination for key in explicit_keys}
    return UnresolvedOptions(
        paths=tuple(paths),
        time_selectors=_tuple_value(values, "time"),
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
        count_grouping=_string_value(values, "count-grouping"),
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
    profile_precedence = resolution.origins["profile"].precedence
    if event_precedence == profile_precedence and event_precedence > 0:
        raise UsageError(
            "--events cannot be combined with --profile at the same precedence layer",
            setting_keys=("events", "profile"),
        )
    if event_precedence > profile_precedence:
        values["profile"] = ()
        explicit_keys.discard("profile")
        event_origin = resolution.origins["events"]
        derived_from_events = EffectiveOrigin((("events", event_origin),), "derived from events")
        origins["profile"] = derived_from_events
    elif profile_precedence > event_precedence and events is not None:
        values["events"] = None
        explicit_keys.discard("events")

    active_events = _optional_tuple(values, "events")
    if active_events is not None:
        event_selection = parse_event_selectors(active_events)
        selection_precedence = resolution.origins["events"].precedence
        selection_origin = EffectiveOrigin((("events", resolution.origins["events"]),), "event selection")
    else:
        profiles = _tuple_value(values, "profile")
        profile = EventProfile(profiles[0] if profiles else EventProfile.GIT.value)
        event_selection = evidence_for_profile(profile)
        selection_precedence = profile_precedence
        selection_origin = EffectiveOrigin((("profile", resolution.origins["profile"]),), "profile selection")
        origins["events"] = EffectiveOrigin(
            (("profile", resolution.origins["profile"]),),
            "profile expansion",
        )

    has_commit_derived_events = any(
        kind.record_kind in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE} for kind in event_selection.kinds
    )
    if not has_commit_derived_events:
        _reset_lower_precedence(
            values,
            origins,
            explicit_keys,
            resolution,
            precedence=selection_precedence,
            effective_origin=selection_origin,
            keys=("git-commits-from",),
        )

    selected_sources = event_selection.sources
    irrelevant = (
        *(("git-commits-from", "git-identity") if Source.GIT not in selected_sources else ()),
        *(("include-ignored", "fs-exclude") if Source.FILESYSTEM not in selected_sources else ()),
    )
    if irrelevant:
        _reset_lower_precedence(
            values,
            origins,
            explicit_keys,
            resolution,
            precedence=selection_precedence,
            effective_origin=selection_origin,
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
