"""Command-line entry point for Wuf."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, cast

from wuf.application.errors import OperationalError
from wuf.application.resolution import validate_without_collection
from wuf.cli.config_display import format_resolved_settings
from wuf.cli.parser import SafeArgumentParser, build_parser, cli_setting_values
from wuf.configuration import (
    EffectiveSettings,
    ResolvedSettings,
    RunOptions,
    UsageError,
    materialize_settings,
    resolve_settings,
)
from wuf.reporting.sanitization import sanitize_terminal_text


class _ReconfigurableTextIO(Protocol):
    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class Invocation:
    """One validated invocation and the settings used to build it."""

    settings: ResolvedSettings
    effective: EffectiveSettings
    show_config: bool

    @property
    def options(self) -> RunOptions:
        """Return the fully materialized run options."""

        return self.effective.options


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
    effective = materialize_settings(resolution, selected_paths)
    show_config = bool(getattr(namespace, "show_config", False))
    if show_config:
        validate_without_collection(effective.options, environ=environment)
    return Invocation(settings=resolution, effective=effective, show_config=show_config)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""

    configure_windows_stdio((sys.stdout, sys.stderr), platform_name=sys.platform)
    try:
        invocation = parse_invocation(argv)
        if invocation.show_config:
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            sys.stdout.write(format_resolved_settings(invocation.settings, invocation.effective, width=width))
            return 0

        from wuf.cli.runner import run

        return run(invocation.options)
    except UsageError as error:
        print(f"error: {sanitize_terminal_text(error)}", file=sys.stderr)
        return 2
    except OperationalError as error:
        print(f"error: {sanitize_terminal_text(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
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
