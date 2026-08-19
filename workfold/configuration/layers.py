"""Shared types and built-in values for layered Workfold settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from workfold.configuration.schema import SettingValue
from workfold.configuration.styles import EventStyleRules


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
    styles: EventStyleRules = EventStyleRules()


@dataclass(frozen=True, slots=True)
class ResolvedStyleLayer:
    """One ordered custom style layer with its configuration provenance."""

    origin: SettingOrigin
    rules: EventStyleRules


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
    style_layers: tuple[ResolvedStyleLayer, ...] = ()


BUILTIN_ORIGIN = SettingOrigin(OriginKind.BUILTIN, 0)

__all__ = [
    "BUILTIN_ORIGIN",
    "ConfigLayer",
    "OriginKind",
    "ResolvedSettings",
    "ResolvedStyleLayer",
    "SettingOrigin",
    "SettingValue",
]
