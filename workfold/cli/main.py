"""Command-line entry point for Workfold."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, cast

from workfold.application.resolution import validate_without_collection
from workfold.cli.config_display import format_resolved_settings
from workfold.cli.parser import SafeArgumentParser, build_parser, cli_setting_values
from workfold.configuration import (
    ResolvedSettings,
    RunOptions,
    UsageError,
    options_from_settings,
    resolve_settings,
)
from workfold.reporting.sanitization import sanitize_terminal_text


class _ReconfigurableTextIO(Protocol):
    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class Invocation:
    """One validated invocation and the settings used to build it."""

    options: RunOptions
    settings: ResolvedSettings
    show_config: bool


def configure_windows_stdio(streams: Sequence[TextIO], *, platform_name: str) -> None:
    """Keep Unicode chart symbols usable in Windows consoles and redirects."""

    if platform_name != "win32":
        return
    for stream in streams:
        if hasattr(stream, "reconfigure"):
            cast(_ReconfigurableTextIO, stream).reconfigure(encoding="utf-8", errors="backslashreplace")


def parse_options(argv: Sequence[str] | None = None) -> RunOptions:
    """Parse options deterministically with built-ins unless a file is explicit."""

    return parse_invocation(argv, automatic_config=False).options


def parse_invocation(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    automatic_config: bool = True,
) -> Invocation:
    """Parse CLI values, layer configuration files, and validate the result."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    namespace = build_parser(suppress_defaults=True).parse_args(arguments)
    working_directory = Path.cwd() if cwd is None else cwd
    environment = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name
    selected_paths = tuple(Path(value) for value in namespace.paths) or (Path("."),)
    explicit_config = getattr(namespace, "config", None)
    resolution = resolve_settings(
        selected_paths,
        cli_setting_values(namespace),
        explicit_config=explicit_config,
        no_config=bool(getattr(namespace, "no_config", False)) or (not automatic_config and explicit_config is None),
        cwd=working_directory,
        environ=environment,
        platform_name=platform,
    )
    options = options_from_settings(resolution, selected_paths)
    show_config = bool(getattr(namespace, "show_config", False))
    if show_config:
        validate_without_collection(options, environ=environment)
    return Invocation(options=options, settings=resolution, show_config=show_config)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""

    configure_windows_stdio((sys.stdout, sys.stderr), platform_name=sys.platform)
    try:
        invocation = parse_invocation(argv)
        if invocation.show_config:
            sys.stdout.write(format_resolved_settings(invocation.settings, invocation.options))
            return 0

        from workfold.cli.runner import run

        return run(invocation.options)
    except UsageError as error:
        print(f"error: {sanitize_terminal_text(error)}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


__all__ = [
    "Invocation",
    "SafeArgumentParser",
    "build_parser",
    "configure_windows_stdio",
    "main",
    "parse_invocation",
    "parse_options",
]
