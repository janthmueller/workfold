"""Command-line entry point for Workfold."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, TextIO, cast

from workfold import __version__
from workfold.config import RawOptions, UsageError
from workfold.sanitization import sanitize_terminal_text
from workfold.settings import (
    ResolvedSettings,
    cli_setting_values,
    format_resolved_settings,
    options_from_settings,
    resolve_settings,
    validate_without_collection,
)


class _ReconfigurableTextIO(Protocol):
    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class Invocation:
    """One validated invocation and the settings used to build it."""

    options: RawOptions
    settings: ResolvedSettings
    show_config: bool


def configure_windows_stdio(streams: Sequence[TextIO], *, platform_name: str) -> None:
    """Keep Unicode chart symbols usable in Windows consoles and redirects."""

    if platform_name != "win32":
        return
    for stream in streams:
        if hasattr(stream, "reconfigure"):
            cast(_ReconfigurableTextIO, stream).reconfigure(encoding="utf-8", errors="backslashreplace")


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes raw terminal control characters."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        safe_message = sanitize_terminal_text(message)
        self.exit(2, f"error: {safe_message}\n")


def build_parser(*, suppress_defaults: bool = False) -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = SafeArgumentParser(
        prog="workfold",
        description=(
            "Fold local Git and filesystem timestamp activity onto a representative week.\n"
            "Timestamps are discrete activity markers, not measured work duration."
        ),
        epilog=(
            "Configuration:\n"
            "  Built-in defaults may be overridden by the global or nearest project\n"
            "  configuration. Use --show-config to inspect effective values and origins.\n\n"
            "Accuracy notes:\n"
            "  Git author and committer dates can differ or be rewritten. File changes\n"
            "  are first-parent tree diffs, not stored human actions. Annotated tags have\n"
            "  tagger dates; lightweight tags do not. Reflogs are local, optional, and\n"
            "  expiring.\n"
            "  Filesystem values are one mutable snapshot: copying, checkout, extraction,\n"
            "  formatting, builds, and reads can change them. Linux birth time uses statx\n"
            "  when returned by the filesystem. ctime is metadata change time; atime may be\n"
            "  unreliable. Deleted untracked files and earlier metadata values cannot be\n"
            "  recovered. Past uncommitted edit sessions require a watcher, which is outside\n"
            "  this MVP. Collection is local and never contacts a remote."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("paths", metavar="PATH", nargs="*", help="repository or filesystem root (built-in default: .)")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    configuration_group = parser.add_argument_group("configuration")
    automatic_config_group = configuration_group.add_mutually_exclusive_group()
    automatic_config_group.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="use exactly FILE instead of automatic global/project discovery",
    )
    automatic_config_group.add_argument(
        "--no-config",
        action="store_true",
        help="ignore global and project configuration files",
    )
    configuration_group.add_argument(
        "--show-config",
        action="store_true",
        help="show effective settings and their origins, then exit",
    )

    date_group = parser.add_argument_group("time selection")
    date_group.add_argument(
        "-t",
        "--time",
        dest="time_selectors",
        action="append",
        default=[],
        metavar="SELECTOR",
        help=(
            "YYYY-MM-DD..YYYY-MM-DD ranges have inclusive endpoints; use "
            "this-week (built-in default), YYYY-Www, a rolling duration such as 2w3d, open ranges, or all; "
            "repeat only ISO weeks"
        ),
    )

    collection_group = parser.add_argument_group("collection scope")
    collection_group.add_argument(
        "-m",
        "--mode",
        dest="modes",
        action="append",
        choices=("git", "fs", "all"),
        default=[],
        help="evidence collector mode (built-in default: git)",
    )
    collection_group.add_argument(
        "-p",
        "--profile",
        dest="profiles",
        action="append",
        choices=("standard", "portable", "full"),
        default=[],
        help=(
            "collection profile: customizable quick defaults for standard (built-in default); Git object-backed "
            "commits and tags with author, committer, and tagger times for portable (Git mode only; no file "
            "changes or reflogs); every supported kind in the selected mode for full (not all time)"
        ),
    )

    git_group = parser.add_argument_group("Git evidence")
    git_group.add_argument(
        "--git-records",
        default=None,
        metavar="KINDS",
        help="comma-separated commit,file-change,tag,reflog (built-in default: commit)",
    )
    git_group.add_argument(
        "--git-commit-times",
        dest="commit_times",
        default=None,
        metavar="KINDS",
        help="comma-separated author,committer (built-in default: author)",
    )
    git_group.add_argument(
        "--git-commits-from",
        dest="commits_from",
        choices=("head", "local-branches", "all-refs"),
        default=None,
        help="commit reachability (standard built-in: local-branches; portable/full: all-refs)",
    )
    git_group.add_argument(
        "--git-identity",
        dest="git_identities",
        action="append",
        default=[],
        metavar="VALUE",
        help=(
            "only include Git timestamps whose recorded author, committer, tagger, or reflog "
            "identity matches VALUE; repeat for OR"
        ),
    )

    filesystem_group = parser.add_argument_group("filesystem evidence")
    filesystem_group.add_argument(
        "--fs-times",
        dest="filesystem_times",
        default=None,
        metavar="KINDS",
        help=(
            "comma-separated timestamps (built-in default: birth,modified): birth, modified, metadata-changed, accessed"
        ),
    )
    filesystem_group.add_argument(
        "--fs-entries",
        dest="filesystem_entries",
        default=None,
        metavar="KINDS",
        help="comma-separated file,directory,symlink (built-in default: file)",
    )
    ignore_group = filesystem_group.add_mutually_exclusive_group()
    ignore_group.add_argument(
        "--respect-gitignore",
        dest="include_ignored",
        action="store_false",
        default=None,
        help="respect standard Git ignore rules (filesystem built-in default)",
    )
    ignore_group.add_argument(
        "--include-ignored",
        dest="include_ignored",
        action="store_true",
        default=None,
        help="include filesystem entries excluded by Git ignore rules",
    )
    filesystem_group.add_argument("--exclude", dest="exclusions", action="append", default=[], metavar="PATTERN")

    output_group = parser.add_argument_group("classification and output")
    output_group.add_argument(
        "--hours",
        metavar="SCHEDULE",
        help="working schedule (built-in default: Mo-Fr 08:00-16:30)",
    )
    output_group.add_argument("--timezone", dest="timezone_name", metavar="IANA_ZONE", help="IANA zone or local")
    output_group.add_argument(
        "--cluster-window",
        default="1h",
        metavar="DURATION",
        help="cluster nearby event times within DURATION (built-in default: 1h; examples: 1m30s, 10m, '1h 5m')",
    )
    output_group.add_argument(
        "--marker-style",
        choices=("source", "identity"),
        default="source",
        help="Git marker labels: source symbol (built-in default) or recorded identity codes",
    )
    output_group.add_argument(
        "--grid",
        dest="grid_style",
        choices=("none", "vertical", "horizontal", "both"),
        default="none",
        help="internal chart lines (built-in default: none)",
    )
    output_group.add_argument("--display-hours", metavar="HH:MM-HH:MM", help="chart crop or auto")
    output_group.add_argument(
        "-H",
        "--hide-days",
        action="append",
        default=[],
        metavar="SCOPE",
        help="always hide weekday columns in weekdays, weekend, or a comma-separated day list; repeatable",
    )
    output_group.add_argument(
        "-E",
        "--hide-empty-days",
        action="append",
        default=[],
        metavar="SCOPE",
        help="hide matching weekday columns only when empty; accepts all, weekdays, weekend, or days; repeatable",
    )
    color_group = output_group.add_mutually_exclusive_group()
    color_group.add_argument("--no-color", action="store_true", default=False)
    color_group.add_argument("--color", dest="no_color", action="store_false", help="allow colored output")
    output_group.add_argument("--list-outside", action=argparse.BooleanOptionalAction, default=False)
    output_group.add_argument("--limit", type=int, default=None, metavar="N")
    output_group.add_argument(
        "--coverage",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show the detailed coverage ledger",
    )
    output_group.add_argument("--strict", action=argparse.BooleanOptionalAction, default=False)
    output_group.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show expanded coverage and operational detail",
    )
    if suppress_defaults:
        for action in parser._actions:
            if action.dest not in {"help", "paths"}:
                action.default = argparse.SUPPRESS
    return parser


def parse_options(argv: Sequence[str] | None = None) -> RawOptions:
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

        from workfold.application import run

        return run(invocation.options)
    except UsageError as error:
        print(f"error: {sanitize_terminal_text(error)}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
