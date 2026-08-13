"""Cross-option validation and collection-profile expansion."""

from __future__ import annotations

from pathlib import Path

from workfold.configuration.options import (
    DEFAULT_CLUSTER_WINDOW,
    DEFAULT_HOURS,
    BandLabel,
    CollectionProfile,
    DisplayHours,
    FilesystemEntry,
    FilesystemTime,
    GitDateMode,
    GitMode,
    GitRecords,
    GridStyle,
    MarkerStyle,
    RefScope,
    RollingDuration,
    RunOptions,
    SourceMode,
    TerminalPreferences,
    UnresolvedOptions,
    UsageError,
)
from workfold.configuration.parsing import (
    parse_clock_minutes,
    parse_cluster_window,
    parse_commit_times,
    parse_display_hours,
    parse_filesystem_entries,
    parse_filesystem_times,
    parse_git_records,
    parse_rolling_duration,
    parse_time_selectors,
    parse_weekday_scopes,
    validate_cluster_options,
    validate_iso_week,
)
from workfold.domain.observations import Weekday
from workfold.folding.bands import ClusterAnchor


def _explicit(values: UnresolvedOptions, name: str) -> bool:
    return name in values.explicit_names


def resolve_options(values: UnresolvedOptions) -> RunOptions:
    """Validate typed setting values and expand evidence presets."""

    weeks, from_date, to_date, all_dates, rolling_duration = parse_time_selectors(values.time_selectors)
    modes = values.modes
    if len(modes) > 1:
        raise UsageError("--mode may be supplied only once")
    mode = modes[0] if modes else "git"
    profiles = values.profiles
    if len(profiles) > 1:
        raise UsageError("--profile may be supplied only once")
    profile = CollectionProfile(profiles[0] if profiles else CollectionProfile.STANDARD.value)
    source = {
        "git": SourceMode.GIT,
        "fs": SourceMode.FILESYSTEM,
        "both": SourceMode.BOTH,
    }[mode]

    if profile is CollectionProfile.PORTABLE and source is not SourceMode.GIT:
        raise UsageError("--profile portable is valid only with --mode git")

    preset = None if profile is CollectionProfile.STANDARD else f"--profile {profile.value}"
    controlled = {
        "--git-records": "git_records",
        "--git-commit-times": "commit_times",
        "--git-commits-from": "commits_from",
        "--fs-times": "filesystem_times",
        "--fs-entries": "filesystem_entries",
        "--include-ignored/--respect-gitignore": "include_ignored",
    }
    if preset is not None:
        conflicts = [flag for flag, name in controlled.items() if _explicit(values, name)]
        if conflicts:
            raise UsageError(f"{preset} controls {', '.join(conflicts)}; remove the scope option(s)")

    if profile is CollectionProfile.FULL:
        git_mode = GitMode.BOTH if source.includes_git else GitMode.COMMITS
        git_date = GitDateMode.BOTH if source.includes_git else GitDateMode.AUTHOR
        git_records = GitRecords.ALL if source.includes_git else GitRecords.COMMITS
        filesystem_times = (
            tuple(FilesystemTime)
            if source.includes_filesystem
            else (
                FilesystemTime.CREATED,
                FilesystemTime.MODIFIED,
            )
        )
        filesystem_entries = tuple(FilesystemEntry) if source.includes_filesystem else (FilesystemEntry.FILE,)
    elif profile is CollectionProfile.PORTABLE:
        git_mode = GitMode.COMMITS
        git_date = GitDateMode.BOTH
        git_records = GitRecords.COMMITS | GitRecords.TAGS
        filesystem_times = (FilesystemTime.CREATED, FilesystemTime.MODIFIED)
        filesystem_entries = (FilesystemEntry.FILE,)
    else:
        git_records_value = values.git_records if values.git_records is not None else "commit"
        commit_times_value = values.commit_times if values.commit_times is not None else "author"
        filesystem_times_value = values.filesystem_times if values.filesystem_times is not None else "birth,modified"
        filesystem_entries_value = values.filesystem_entries if values.filesystem_entries is not None else "file"
        git_mode, git_records = parse_git_records(git_records_value)
        git_date = parse_commit_times(commit_times_value)
        filesystem_times = parse_filesystem_times(filesystem_times_value)
        filesystem_entries = parse_filesystem_entries(filesystem_entries_value)

    if values.commits_from is not None:
        ref_scope = {
            "head": RefScope.HEAD,
            "local-branches": RefScope.LOCAL_BRANCHES,
            "all-refs": RefScope.ALL_REFS,
        }[values.commits_from]
    elif profile in {CollectionProfile.PORTABLE, CollectionProfile.FULL}:
        ref_scope = RefScope.ALL_REFS
    else:
        ref_scope = RefScope.LOCAL_BRANCHES

    explicit_git = any(_explicit(values, name) for name in ("git_records", "commit_times", "commits_from")) or bool(
        values.git_identities
    )
    explicit_filesystem = any(
        _explicit(values, name) for name in ("filesystem_times", "filesystem_entries", "include_ignored")
    ) or bool(values.exclusions)
    if source is SourceMode.FILESYSTEM and explicit_git:
        raise UsageError("Git-specific options cannot be used with --mode fs")
    if source is SourceMode.GIT and explicit_filesystem:
        raise UsageError("filesystem-specific options cannot be used with --mode git")

    if not git_records.includes_commits:
        irrelevant: list[str] = []
        if _explicit(values, "commit_times"):
            irrelevant.append("--git-commit-times")
        if _explicit(values, "commits_from"):
            irrelevant.append("--git-commits-from")
        if irrelevant:
            raise UsageError(f"{', '.join(irrelevant)} require commit or file-change records to be enabled")

    git_identities = tuple(value.strip() for value in values.git_identities)
    if any(not value for value in git_identities):
        raise UsageError("--git-identity values cannot be empty")
    exclusions = tuple(value.strip() for value in values.exclusions)
    if any(not value for value in exclusions):
        raise UsageError("--exclude values cannot be empty")
    if any(value.startswith("!") for value in exclusions):
        raise UsageError("--exclude patterns cannot be negated; explicit exclusions always win")

    if _explicit(values, "limit") and not values.list_outside:
        raise UsageError("--limit requires --list-outside")
    limit = values.limit
    if limit < 1:
        raise UsageError("--limit must be at least 1")

    include_ignored = bool((profile is CollectionProfile.FULL and source.includes_filesystem) or values.include_ignored)
    respect_gitignore = not include_ignored

    hide_days = parse_weekday_scopes(values.hide_days, option="--hide-days")
    if hide_days == tuple(Weekday):
        raise UsageError("--hide-days cannot hide all seven weekday columns")
    hide_empty_days = parse_weekday_scopes(values.hide_empty_days, option="--hide-empty-days")

    cluster_window = parse_cluster_window(values.cluster_window)
    cluster_anchor = ClusterAnchor(values.cluster_anchor)
    validate_cluster_options(
        cluster_window,
        cluster_anchor,
        show_empty_bands=values.show_empty_bands,
    )

    paths = values.paths or (Path("."),)
    return RunOptions(
        paths=paths,
        weeks=weeks,
        from_date=from_date,
        to_date=to_date,
        all_dates=all_dates,
        rolling_duration=rolling_duration,
        profile=profile,
        source=source,
        git_mode=git_mode,
        git_date=git_date,
        git_records=git_records,
        ref_scope=ref_scope,
        git_identities=git_identities,
        filesystem_times=filesystem_times,
        filesystem_entries=filesystem_entries,
        include_ignored=include_ignored,
        respect_gitignore=respect_gitignore,
        exclusions=exclusions,
        hours=values.hours or DEFAULT_HOURS,
        timezone_name=values.timezone_name,
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        display_hours=parse_display_hours(values.display_hours) if values.display_hours else None,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
        terminal=TerminalPreferences(
            marker_style=MarkerStyle(values.marker_style),
            grid_style=GridStyle(values.grid_style),
            band_label=BandLabel(values.band_label),
            show_empty_bands=values.show_empty_bands,
            no_color=values.no_color,
            list_outside=values.list_outside,
            outside_limit=limit,
            coverage=values.coverage,
            strict=values.strict,
            verbose=values.verbose,
        ),
    )


__all__ = [
    "DEFAULT_CLUSTER_WINDOW",
    "DEFAULT_HOURS",
    "BandLabel",
    "ClusterAnchor",
    "CollectionProfile",
    "DisplayHours",
    "FilesystemEntry",
    "FilesystemTime",
    "GitDateMode",
    "GitMode",
    "GitRecords",
    "GridStyle",
    "MarkerStyle",
    "RunOptions",
    "RefScope",
    "RollingDuration",
    "SourceMode",
    "TerminalPreferences",
    "UnresolvedOptions",
    "UsageError",
    "parse_clock_minutes",
    "parse_cluster_window",
    "parse_commit_times",
    "parse_display_hours",
    "parse_filesystem_entries",
    "parse_filesystem_times",
    "parse_git_records",
    "parse_rolling_duration",
    "parse_time_selectors",
    "parse_weekday_scopes",
    "resolve_options",
    "validate_iso_week",
]
