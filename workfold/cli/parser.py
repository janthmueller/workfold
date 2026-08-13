"""Safe argparse declaration for Workfold's public CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, cast

from workfold import __version__
from workfold.configuration.layers import SettingValue
from workfold.reporting.sanitization import sanitize_terminal_text


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes raw terminal control characters."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"error: {sanitize_terminal_text(message)}\n")


def cli_setting_values(namespace: argparse.Namespace) -> dict[str, SettingValue]:
    """Convert only explicitly parsed CLI arguments to canonical settings."""

    raw = vars(namespace)
    values: dict[str, SettingValue] = {}
    sequence_mapping = {
        "time_selectors": "time",
        "modes": "mode",
        "profiles": "profile",
        "git_identities": "git-identity",
        "exclusions": "exclude",
        "hide_days": "hide-days",
        "hide_empty_days": "hide-empty-days",
    }
    for destination, key in sequence_mapping.items():
        if destination in raw:
            values[key] = tuple(cast(Sequence[str], raw[destination]))

    csv_mapping = {
        "git_records": "git-records",
        "commit_times": "git-commit-times",
        "filesystem_times": "fs-times",
        "filesystem_entries": "fs-entries",
    }
    for destination, key in csv_mapping.items():
        if destination in raw:
            values[key] = tuple(cast(str, raw[destination]).split(","))

    scalar_mapping = {
        "commits_from": "git-commits-from",
        "include_ignored": "include-ignored",
        "hours": "hours",
        "timezone_name": "timezone",
        "cluster_window": "cluster-window",
        "cluster_anchor": "cluster-anchor",
        "band_label": "band-label",
        "show_empty_bands": "show-empty-bands",
        "marker_style": "marker-style",
        "grid_style": "grid",
        "display_hours": "display-hours",
        "no_color": "no-color",
        "list_outside": "list-outside",
        "limit": "limit",
        "coverage": "coverage",
        "strict": "strict",
        "verbose": "verbose",
    }
    for destination, key in scalar_mapping.items():
        if destination in raw:
            values[key] = cast(SettingValue, raw[destination])
    return values


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
        choices=("git", "fs", "both"),
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
            "evidence preset: customizable low-noise defaults for standard (built-in default); Git object-backed "
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
    filesystem_group.add_argument(
        "--exclude",
        dest="exclusions",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "exclude root-relative filesystem paths using Git-style patterns; repeatable; "
            "explicit exclusions always win"
        ),
    )

    output_group = parser.add_argument_group("classification and output")
    output_group.add_argument(
        "--hours",
        metavar="SCHEDULE",
        help="working schedule (built-in default: Mo-Fr 08:00-16:30; all means every minute of all seven days)",
    )
    output_group.add_argument("--timezone", dest="timezone_name", metavar="IANA_ZONE", help="IANA zone or local")
    output_group.add_argument(
        "--cluster-window",
        default="1h",
        metavar="DURATION",
        help=(
            "cluster nearby event times within DURATION and use it as the compressed-gap threshold "
            "(built-in default: 1h; examples: 1m30s, 10m, '1h 5m')"
        ),
    )
    output_group.add_argument(
        "--cluster-anchor",
        choices=("event", "midnight"),
        default="event",
        help=(
            "anchor clusters at each first event (built-in default) or fixed intervals from local midnight; "
            "midnight requires whole-minute windows"
        ),
    )
    output_group.add_argument(
        "--band-label",
        choices=("range", "start"),
        default="range",
        help=(
            "show each occupied row as an observed/fixed range (built-in default) or its starting minute; "
            "explicitly clipped dense edges keep exact ranges"
        ),
    )
    output_group.add_argument(
        "--show-empty-bands",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show every fixed band in the display range; requires --cluster-anchor midnight",
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
    color_group.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="never emit color (built-in default: automatic terminal and NO_COLOR detection)",
    )
    color_group.add_argument(
        "--color",
        dest="no_color",
        action="store_false",
        help="allow automatic color, respecting terminal detection and NO_COLOR",
    )
    output_group.add_argument(
        "--list-outside",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="append a chronological list of events outside working hours",
    )
    output_group.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="maximum outside-hours rows (built-in default: 50; requires --list-outside)",
    )
    output_group.add_argument(
        "--coverage",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show the detailed coverage ledger without verbose scope details",
    )
    output_group.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="return non-zero when collection is incomplete",
    )
    output_group.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show scope and operational details plus the detailed coverage ledger",
    )
    if suppress_defaults:
        for action in parser._actions:
            if action.dest not in {"help", "paths"}:
                action.default = argparse.SUPPRESS
    return parser


__all__ = ["SafeArgumentParser", "build_parser", "cli_setting_values"]
