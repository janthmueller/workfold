"""Shared types and built-in values for layered Workfold settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from workfold.config import DEFAULT_HOURS

SettingValue: TypeAlias = str | bool | int | tuple[str, ...] | None


class OriginKind(str, Enum):
    """One precedence layer that supplied a setting."""

    BUILTIN = "built-in"
    GLOBAL = "global"
    LOCAL = "local"
    EXPLICIT = "config"
    CLI = "CLI"


@dataclass(frozen=True, slots=True)
class SettingOrigin:
    """The source and precedence of one effective setting."""

    kind: OriginKind
    precedence: int
    path: Path | None = None

    @property
    def label(self) -> str:
        return self.kind.value


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    """One validated TOML configuration layer."""

    path: Path
    values: Mapping[str, SettingValue]


@dataclass(frozen=True, slots=True)
class ResolvedSettings:
    """Merged CLI-compatible settings with per-key provenance."""

    values: Mapping[str, SettingValue]
    origins: Mapping[str, SettingOrigin]
    global_candidate: Path | None
    global_config: Path | None
    local_config: Path | None
    explicit_config: Path | None
    config_disabled: bool


BUILTIN_ORIGIN = SettingOrigin(OriginKind.BUILTIN, 0)

DEFAULT_SETTINGS: dict[str, SettingValue] = {
    "time": ("this-week",),
    "mode": ("git",),
    "profile": ("standard",),
    "git-records": ("commit",),
    "git-commit-times": ("author",),
    "git-commits-from": None,
    "git-identity": (),
    "fs-times": ("birth", "modified"),
    "fs-entries": ("file",),
    "include-ignored": False,
    "exclude": (),
    "hours": DEFAULT_HOURS,
    "timezone": None,
    "cluster-window": "1h",
    "marker-style": "source",
    "grid": "none",
    "display-hours": None,
    "hide-days": (),
    "hide-empty-days": (),
    "no-color": False,
    "list-outside": False,
    "limit": 50,
    "coverage": False,
    "strict": False,
    "verbose": False,
}


__all__ = [
    "BUILTIN_ORIGIN",
    "DEFAULT_SETTINGS",
    "ConfigLayer",
    "OriginKind",
    "ResolvedSettings",
    "SettingOrigin",
    "SettingValue",
]
