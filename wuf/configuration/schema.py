"""Canonical mechanical schema for every configurable Wuf setting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from wuf.configuration.options import DEFAULT_HOURS
from wuf.configuration.profiles import EventProfile

SettingValue: TypeAlias = str | bool | int | tuple[str, ...] | None


class ConfigShape(str, Enum):
    """TOML representation accepted for one setting."""

    STRING = "string"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    STRING_ARRAY = "string_array"
    STRING_OR_ARRAY = "string_or_array"
    SINGLE_STRING_TUPLE = "single_string_tuple"

    @property
    def cli_sequence(self) -> bool:
        """Return whether argparse supplies this setting as a sequence."""

        return self in {
            ConfigShape.STRING_ARRAY,
            ConfigShape.STRING_OR_ARRAY,
            ConfigShape.SINGLE_STRING_TUPLE,
        }


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Stable key, representation, and CLI binding for one setting."""

    key: str
    cli_destination: str
    default: SettingValue
    config_shape: ConfigShape
    choices: tuple[str, ...] = ()


SETTING_SPECS = (
    SettingSpec("time", "time_selectors", ("this-week",), ConfigShape.STRING_OR_ARRAY),
    SettingSpec(
        "profile",
        "profiles",
        (EventProfile.GIT.value,),
        ConfigShape.SINGLE_STRING_TUPLE,
        tuple(profile.value for profile in EventProfile),
    ),
    SettingSpec("events", "event_selectors", None, ConfigShape.STRING_ARRAY),
    SettingSpec(
        "git-commits-from",
        "commits_from",
        None,
        ConfigShape.STRING,
        ("head", "local-branches", "all-refs"),
    ),
    SettingSpec("git-identity", "git_identities", (), ConfigShape.STRING_ARRAY),
    SettingSpec("include-ignored", "include_ignored", False, ConfigShape.BOOLEAN),
    SettingSpec("fs-exclude", "exclusions", (), ConfigShape.STRING_ARRAY),
    SettingSpec("hours", "hours", DEFAULT_HOURS, ConfigShape.STRING),
    SettingSpec("timezone", "timezone_name", None, ConfigShape.STRING),
    SettingSpec("cluster-window", "cluster_window", "1h", ConfigShape.STRING),
    SettingSpec("cluster-anchor", "cluster_anchor", "event", ConfigShape.STRING, ("event", "midnight")),
    SettingSpec("band-label", "band_label", "range", ConfigShape.STRING, ("range", "start")),
    SettingSpec("show-empty-bands", "show_empty_bands", False, ConfigShape.BOOLEAN),
    SettingSpec("marker-style", "marker_style", "source", ConfigShape.STRING, ("source", "identity")),
    SettingSpec("count-grouping", "count_grouping", "event", ConfigShape.STRING, ("event", "visual")),
    SettingSpec("grid", "grid_style", "none", ConfigShape.STRING, ("none", "vertical", "horizontal", "both")),
    SettingSpec("display-hours", "display_hours", None, ConfigShape.STRING),
    SettingSpec("hide-days", "hide_days", (), ConfigShape.STRING_ARRAY),
    SettingSpec("hide-empty-days", "hide_empty_days", (), ConfigShape.STRING_ARRAY),
    SettingSpec("no-color", "no_color", False, ConfigShape.BOOLEAN),
    SettingSpec("list", "list_selectors", (), ConfigShape.STRING_ARRAY),
    SettingSpec("limit", "limit", 50, ConfigShape.INTEGER),
    SettingSpec("coverage", "coverage", False, ConfigShape.BOOLEAN),
    SettingSpec("strict", "strict", False, ConfigShape.BOOLEAN),
    SettingSpec("verbose", "verbose", False, ConfigShape.BOOLEAN),
)

SETTING_BY_KEY = MappingProxyType({spec.key: spec for spec in SETTING_SPECS})
SETTING_BY_DESTINATION = MappingProxyType({spec.cli_destination: spec for spec in SETTING_SPECS})
DEFAULT_SETTINGS = MappingProxyType({spec.key: spec.default for spec in SETTING_SPECS})

if len(SETTING_BY_KEY) != len(SETTING_SPECS):
    raise RuntimeError("Wuf setting keys must be unique")
if len(SETTING_BY_DESTINATION) != len(SETTING_SPECS):
    raise RuntimeError("Wuf setting CLI destinations must be unique")


__all__ = [
    "DEFAULT_SETTINGS",
    "SETTING_BY_DESTINATION",
    "SETTING_BY_KEY",
    "SETTING_SPECS",
    "ConfigShape",
    "SettingSpec",
    "SettingValue",
]
