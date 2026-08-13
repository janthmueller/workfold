"""TOML parsing, configuration discovery, and precedence merging."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from workfold.configuration.layers import (
    BUILTIN_ORIGIN,
    DEFAULT_SETTINGS,
    ConfigLayer,
    OriginKind,
    ResolvedSettings,
    SettingOrigin,
    SettingValue,
)
from workfold.configuration.options import UsageError

_STRING_KEYS = frozenset(
    {
        "git-commits-from",
        "hours",
        "timezone",
        "cluster-window",
        "cluster-anchor",
        "band-label",
        "marker-style",
        "grid",
        "display-hours",
    }
)
_BOOLEAN_KEYS = frozenset(
    {
        "include-ignored",
        "no-color",
        "list-outside",
        "coverage",
        "strict",
        "verbose",
        "show-empty-bands",
    }
)
_INTEGER_KEYS = frozenset({"limit"})
_ARRAY_KEYS = frozenset(
    {
        "git-records",
        "git-commit-times",
        "git-identity",
        "fs-times",
        "fs-entries",
        "exclude",
        "hide-days",
        "hide-empty-days",
    }
)
_SCALAR_ARRAY_KEYS = frozenset({"mode", "profile"})
_CONFIG_KEYS = frozenset(DEFAULT_SETTINGS)
_CHOICES: dict[str, frozenset[str]] = {
    "mode": frozenset({"git", "fs", "both"}),
    "profile": frozenset({"standard", "portable", "full"}),
    "git-records": frozenset({"commit", "file-change", "tag", "reflog"}),
    "git-commit-times": frozenset({"author", "committer"}),
    "git-commits-from": frozenset({"head", "local-branches", "all-refs"}),
    "fs-times": frozenset({"birth", "modified", "metadata-changed", "accessed"}),
    "fs-entries": frozenset({"file", "directory", "symlink"}),
    "cluster-anchor": frozenset({"event", "midnight"}),
    "band-label": frozenset({"range", "start"}),
    "marker-style": frozenset({"source", "identity"}),
    "grid": frozenset({"none", "vertical", "horizontal", "both"}),
}
_PYPROJECT_TABLE_HEADER = re.compile(rb"(?m)^[ \t]*\[[ \t]*tool[ \t]*\.[ \t]*workfold[ \t]*\]")


def resolve_settings(
    paths: Sequence[Path],
    cli_values: Mapping[str, SettingValue],
    *,
    explicit_config: Path | None,
    no_config: bool,
    cwd: Path,
    environ: Mapping[str, str],
    platform_name: str,
) -> ResolvedSettings:
    """Resolve built-ins, automatic files, and explicit CLI values."""

    values = dict(DEFAULT_SETTINGS)
    origins = {key: BUILTIN_ORIGIN for key in values}
    global_candidate: Path | None = None
    global_layer: ConfigLayer | None = None
    local_layer: ConfigLayer | None = None
    explicit_layer: ConfigLayer | None = None

    if explicit_config is not None and no_config:
        raise UsageError("--config and --no-config are mutually exclusive")

    if explicit_config is not None:
        config_path = _absolute_path(explicit_config, cwd)
        explicit_layer = _load_config(config_path, required=True)
        assert explicit_layer is not None
        _apply_layer(values, origins, explicit_layer, SettingOrigin(OriginKind.EXPLICIT, 2, config_path))
    elif not no_config:
        global_candidate = global_config_path(environ=environ, platform_name=platform_name)
        global_layer = _load_config(global_candidate, required=False)
        if global_layer is not None:
            _apply_layer(
                values,
                origins,
                global_layer,
                SettingOrigin(OriginKind.GLOBAL, 1, global_layer.path),
            )

        local_layer = _resolve_local_layer(paths, cwd=cwd)
        if local_layer is not None:
            _apply_layer(
                values,
                origins,
                local_layer,
                SettingOrigin(OriginKind.LOCAL, 2, local_layer.path),
            )

    cli_origin = SettingOrigin(OriginKind.CLI, 3)
    for key, value in cli_values.items():
        values[key] = value
        origins[key] = cli_origin

    return ResolvedSettings(
        values=values,
        origins=origins,
        global_candidate=global_candidate,
        global_config=global_layer.path if global_layer is not None else None,
        local_config=local_layer.path if local_layer is not None else None,
        explicit_config=explicit_layer.path if explicit_layer is not None else None,
        config_disabled=no_config,
    )


def global_config_path(*, environ: Mapping[str, str], platform_name: str) -> Path:
    """Return the platform's automatic per-user Workfold configuration path."""

    home = _home_directory(environ, platform_name=platform_name)
    if platform_name == "win32":
        configured_text = environ.get("APPDATA", "")
        configured = (
            Path(configured_text) if configured_text and PureWindowsPath(configured_text).is_absolute() else None
        )
        base = configured if configured is not None else home / "AppData" / "Roaming"
    elif platform_name == "darwin":
        base = home / "Library" / "Application Support"
    else:
        configured = environ.get("XDG_CONFIG_HOME", "")
        candidate = Path(configured) if configured else None
        base = candidate if candidate is not None and PurePosixPath(configured).is_absolute() else home / ".config"
    return base / "workfold" / "workfold.toml"


def _resolve_local_layer(paths: Sequence[Path], *, cwd: Path) -> ConfigLayer | None:
    discovered: dict[Path | None, list[Path]] = {}
    resolved_layers: dict[Path, ConfigLayer] = {}
    loaded_files: dict[Path, ConfigLayer | None] = {}
    nearest_by_directory: dict[Path, ConfigLayer | None] = {}
    for selected_path in paths:
        absolute = _absolute_path(selected_path, cwd)
        layer = _find_local_config(
            absolute,
            loaded_files=loaded_files,
            nearest_by_directory=nearest_by_directory,
        )
        key = layer.path if layer is not None else None
        if layer is not None:
            resolved_layers[layer.path] = layer
        discovered.setdefault(key, []).append(absolute)

    if len(discovered) > 1:
        assignments: list[str] = []
        for config_path, selected_paths in discovered.items():
            config_label = str(config_path) if config_path is not None else "no local config"
            assignments.append(f"{', '.join(str(path) for path in selected_paths)} -> {config_label}")
        raise UsageError(
            "selected paths resolve to different local Workfold configurations; "
            "use --config or --no-config: " + "; ".join(assignments)
        )
    if not discovered:
        return None
    only_path = next(iter(discovered))
    return resolved_layers.get(only_path) if only_path is not None else None


def _find_local_config(
    path: Path,
    *,
    loaded_files: dict[Path, ConfigLayer | None],
    nearest_by_directory: dict[Path, ConfigLayer | None],
) -> ConfigLayer | None:
    start = path if path.is_dir() else path.parent
    visited: list[Path] = []
    result: ConfigLayer | None = None
    for directory in (start, *start.parents):
        if directory in nearest_by_directory:
            result = nearest_by_directory[directory]
            break
        visited.append(directory)

        standalone = directory / "workfold.toml"
        if standalone.is_file():
            if standalone in loaded_files:
                result = loaded_files[standalone]
            else:
                loaded_layer = _load_config(standalone, required=True)
                assert loaded_layer is not None
                loaded_files[standalone] = loaded_layer
                result = loaded_layer
            break

        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            if pyproject not in loaded_files:
                loaded_files[pyproject] = _load_config(
                    pyproject,
                    required=False,
                    require_workfold_table=False,
                )
            result = loaded_files[pyproject]
            if result is not None:
                break
    for directory in visited:
        nearest_by_directory[directory] = result
    return result


def _load_config(
    path: Path,
    *,
    required: bool,
    require_workfold_table: bool = True,
) -> ConfigLayer | None:
    if not path.is_file():
        if required:
            raise UsageError(f"configuration file does not exist: {path}")
        return None
    try:
        content = path.read_bytes()
    except OSError as error:
        raise UsageError(f"cannot read configuration file {path}: {error}") from error
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        if path.name == "pyproject.toml" and not required and not _contains_workfold_table(content):
            # An unrelated malformed pyproject must not block discovery of a
            # Workfold configuration farther up the selected path. Valid TOML
            # is always parsed semantically, including quoted or dotted keys.
            return None
        raise UsageError(f"invalid TOML in configuration file {path}: {error}") from error

    if path.name == "pyproject.toml":
        table = _pyproject_table(document, path=path)
        if table is None:
            if required and require_workfold_table:
                raise UsageError(f"configuration file {path} has no [tool.workfold] table")
            return None
    else:
        table = cast(dict[str, object], document)
    return ConfigLayer(path.resolve(), _validate_table(table, path=path))


def _contains_workfold_table(content: bytes) -> bool:
    """Conservatively recognize a literal table in malformed TOML."""

    return _PYPROJECT_TABLE_HEADER.search(content) is not None


def _pyproject_table(document: Mapping[str, object], *, path: Path) -> dict[str, object] | None:
    tool = document.get("tool")
    if tool is None:
        return None
    if not isinstance(tool, dict):
        raise UsageError(f"[tool] must be a table in configuration file {path}")
    tool_table = cast(dict[str, object], tool)
    workfold = tool_table.get("workfold")
    if workfold is None:
        return None
    if not isinstance(workfold, dict):
        raise UsageError(f"[tool.workfold] must be a table in configuration file {path}")
    return cast(dict[str, object], workfold)


def _validate_table(table: Mapping[str, object], *, path: Path) -> dict[str, SettingValue]:
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        label = ", ".join(repr(key) for key in unknown)
        raise UsageError(f"unknown Workfold configuration key(s) in {path}: {label}")
    return {key: _validate_value(key, value, path=path) for key, value in table.items()}


def _validate_value(key: str, value: object, *, path: Path) -> SettingValue:
    location = f"{path}: {key}"
    if key == "time":
        if isinstance(value, str):
            return (value,)
        return _string_array(value, location=location)
    if key in _SCALAR_ARRAY_KEYS:
        if not isinstance(value, str):
            raise UsageError(f"{location} must be a string")
        _validate_choice(key, value, location=location)
        return (value,)
    if key in _STRING_KEYS:
        if not isinstance(value, str):
            raise UsageError(f"{location} must be a string")
        _validate_choice(key, value, location=location)
        return value
    if key in _BOOLEAN_KEYS:
        if not isinstance(value, bool):
            raise UsageError(f"{location} must be true or false")
        return value
    if key in _INTEGER_KEYS:
        if not isinstance(value, int) or isinstance(value, bool):
            raise UsageError(f"{location} must be an integer")
        return value
    if key in _ARRAY_KEYS:
        items = _string_array(value, location=location)
        for item in items:
            _validate_choice(key, item, location=location)
        return items
    raise AssertionError(f"unhandled Workfold configuration key: {key}")


def _string_array(value: object, *, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise UsageError(f"{location} must be an array of strings")
    items = cast(list[object], value)
    if any(not isinstance(item, str) for item in items):
        raise UsageError(f"{location} must be an array of strings")
    return tuple(cast(str, item) for item in items)


def _validate_choice(key: str, value: str, *, location: str) -> None:
    choices = _CHOICES.get(key)
    if choices is not None and value not in choices:
        raise UsageError(f"{location} must be one of {', '.join(sorted(choices))}; got {value!r}")


def _apply_layer(
    values: dict[str, SettingValue],
    origins: dict[str, SettingOrigin],
    layer: ConfigLayer,
    origin: SettingOrigin,
) -> None:
    for key, value in layer.values.items():
        values[key] = value
        origins[key] = origin


def _home_directory(environ: Mapping[str, str], *, platform_name: str) -> Path:
    variable = "USERPROFILE" if platform_name == "win32" else "HOME"
    configured = environ.get(variable)
    return Path(configured) if configured else Path.home()


def _absolute_path(path: Path, cwd: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return Path(os.path.abspath(candidate))


__all__ = ["global_config_path", "resolve_settings"]
