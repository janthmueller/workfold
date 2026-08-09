"""Command-line entry point for Workfold."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn, Protocol, TextIO, cast

from workfold import __version__
from workfold.config import RawOptions, UsageError, options_from_namespace
from workfold.sanitization import sanitize_terminal_text


class _ReconfigurableTextIO(Protocol):
    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None: ...


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
        self.exit(2, f"{self.prog}: error: {safe_message}\n")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = SafeArgumentParser(
        prog="workfold",
        description=(
            "Fold local Git and filesystem timestamp activity onto a representative week.\n"
            "Timestamps are discrete activity markers, not measured work duration."
        ),
        epilog=(
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
    parser.add_argument("paths", metavar="PATH", nargs="*", help="repository or filesystem root (default: .)")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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
            "this-week (default), YYYY-Www, open ranges, or all; repeat only ISO weeks"
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
        help="evidence collector mode (default: git)",
    )
    collection_group.add_argument(
        "-p",
        "--profile",
        dest="profiles",
        action="append",
        choices=("standard", "portable", "full"),
        default=[],
        help=(
            "collection profile: customizable quick defaults for standard (default); Git object-backed "
            "commits and tags with author, committer, and tagger times for portable (Git mode only; no file "
            "changes or reflogs); every supported kind in the selected mode for full (not all time)"
        ),
    )

    git_group = parser.add_argument_group("Git evidence")
    git_group.add_argument(
        "--git-records",
        default=None,
        metavar="KINDS",
        help="comma-separated commit,file-change,tag,reflog (default: commit)",
    )
    git_group.add_argument(
        "--git-commit-times",
        dest="commit_times",
        default=None,
        metavar="KINDS",
        help="comma-separated author,committer (default: author)",
    )
    git_group.add_argument(
        "--git-commits-from",
        dest="commits_from",
        choices=("HEAD", "all-local-refs"),
        default=None,
        help="commit reachability (default: all-local-refs)",
    )
    git_group.add_argument("--author", dest="authors", action="append", default=[], metavar="VALUE")

    filesystem_group = parser.add_argument_group("filesystem evidence")
    filesystem_group.add_argument(
        "--fs-times",
        dest="filesystem_times",
        default=None,
        metavar="KINDS",
        help=("comma-separated timestamps (default: birth,modified): birth, modified, metadata-changed, accessed"),
    )
    filesystem_group.add_argument(
        "--fs-entries",
        dest="filesystem_entries",
        default=None,
        metavar="KINDS",
        help="comma-separated file,directory,symlink (default: file)",
    )
    ignore_group = filesystem_group.add_mutually_exclusive_group()
    ignore_group.add_argument(
        "--respect-gitignore",
        action="store_true",
        default=None,
        help="respect standard Git ignore rules (filesystem default)",
    )
    ignore_group.add_argument(
        "--include-ignored",
        action="store_true",
        default=None,
        help="include filesystem entries excluded by Git ignore rules",
    )
    filesystem_group.add_argument("--exclude", dest="exclusions", action="append", default=[], metavar="PATTERN")

    output_group = parser.add_argument_group("classification and output")
    output_group.add_argument("--hours", metavar="SCHEDULE", help="working schedule (default: Mo-Fr 08:00-16:30)")
    output_group.add_argument("--timezone", dest="timezone_name", metavar="IANA_ZONE")
    output_group.add_argument(
        "--cluster-window",
        default="1h",
        metavar="DURATION",
        help="cluster nearby event times within DURATION (default: 1h; examples: 30s, 10m, '1h 5m')",
    )
    output_group.add_argument("--display-hours", metavar="HH:MM-HH:MM")
    output_group.add_argument("--no-color", action="store_true")
    output_group.add_argument("--list-outside", action="store_true")
    output_group.add_argument("--limit", type=int, default=None, metavar="N")
    output_group.add_argument(
        "--coverage",
        action="store_true",
        help="show the detailed coverage ledger",
    )
    output_group.add_argument("--strict", action="store_true")
    output_group.add_argument(
        "--verbose",
        action="store_true",
        help="show expanded coverage and operational detail",
    )
    return parser


def parse_options(argv: Sequence[str] | None = None) -> RawOptions:
    """Parse and validate command-line options."""
    return options_from_namespace(build_parser().parse_args(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    configure_windows_stdio((sys.stdout, sys.stderr), platform_name=sys.platform)
    try:
        options = parse_options(argv)
        from workfold.application import run

        return run(options)
    except UsageError as error:
        print(f"workfold: error: {sanitize_terminal_text(error)}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
