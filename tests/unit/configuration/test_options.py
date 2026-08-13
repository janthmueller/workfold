from __future__ import annotations

from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path

import pytest
from workfold.cli import parse_options
from workfold.configuration import (
    BandLabel,
    ClusterAnchor,
    CollectionProfile,
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
    parse_clock_minutes,
    parse_cluster_window,
    parse_display_hours,
    parse_filesystem_entries,
    parse_filesystem_times,
    parse_git_records,
    parse_rolling_duration,
    parse_time_selectors,
    parse_weekday_scopes,
    validate_iso_week,
)
from workfold.domain.observations import Weekday


def test_default_options_are_the_quick_git_view_for_this_week() -> None:
    options = parse_options([])

    assert options.paths == (Path("."),)
    assert options.source is SourceMode.GIT
    assert options.weeks == ()
    assert options.from_date is None
    assert options.to_date is None
    assert not options.all_dates
    assert options.rolling_duration is None
    assert options.profile is CollectionProfile.STANDARD
    assert options.git_mode is GitMode.COMMITS
    assert options.git_date is GitDateMode.AUTHOR
    assert options.git_records == GitRecords.COMMITS
    assert options.ref_scope is RefScope.LOCAL_BRANCHES
    assert options.git_identities == ()
    assert options.filesystem_times == (FilesystemTime.CREATED, FilesystemTime.MODIFIED)
    assert options.filesystem_entries == (FilesystemEntry.FILE,)
    assert options.respect_gitignore
    assert options.cluster_window == timedelta(hours=1)
    assert options.cluster_anchor is ClusterAnchor.EVENT
    assert options.terminal.band_label is BandLabel.RANGE
    assert not options.terminal.show_empty_bands
    assert options.terminal.marker_style is MarkerStyle.SOURCE
    assert options.terminal.grid_style is GridStyle.NONE
    assert options.hide_days == ()
    assert options.hide_empty_days == ()


def test_terminal_preferences_are_grouped_away_from_collection_options() -> None:
    resolved = parse_options([])
    run_names = tuple(field.name for field in fields(RunOptions))
    unresolved_names = tuple(field.name for field in fields(UnresolvedOptions))
    terminal_names = tuple(field.name for field in fields(TerminalPreferences))

    assert run_names[-2:] == ("terminal", "cluster_anchor")
    assert not set(terminal_names) & set(run_names)
    assert terminal_names == (
        "marker_style",
        "grid_style",
        "band_label",
        "show_empty_bands",
        "no_color",
        "list_outside",
        "outside_limit",
        "coverage",
        "strict",
        "verbose",
    )
    assert unresolved_names[-6:] == (
        "strict",
        "verbose",
        "explicit_names",
        "cluster_anchor",
        "band_label",
        "show_empty_bands",
    )
    assert resolved.terminal.outside_limit == 50


def test_explicit_this_week_matches_the_implicit_default() -> None:
    implicit = parse_options([])
    explicit = parse_options(["--time", "this-week"])

    assert explicit.weeks == implicit.weeks
    assert explicit.from_date == implicit.from_date
    assert explicit.to_date == implicit.to_date
    assert explicit.all_dates == implicit.all_dates


def test_color_flags_enable_or_disable_the_automatic_color_policy() -> None:
    automatic = parse_options([])
    enabled = parse_options(["--color"])
    disabled = parse_options(["--no-color"])

    assert not automatic.terminal.no_color
    assert not enabled.terminal.no_color
    assert disabled.terminal.no_color


def test_repeated_iso_weeks_form_the_only_supported_selector_union() -> None:
    options = parse_options(["-t", "2026-W30", "--time", "2026-W31", "-t", "2026-W30"])

    assert options.weeks == ("2026-W30", "2026-W31")


@pytest.mark.parametrize(
    "selectors",
    [
        ["this-week", "this-week"],
        ["this-week", "2026-W31"],
        ["all", "2026-W31"],
        ["2026-07-01..2026-07-31", "2026-W31"],
    ],
)
def test_only_iso_weeks_may_be_repeated(selectors: list[str]) -> None:
    arguments = [item for selector in selectors for item in ("--time", selector)]

    with pytest.raises(UsageError, match="repeated only"):
        parse_options(arguments)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("2026-07-01..2026-07-31", (date(2026, 7, 1), date(2026, 7, 31), False)),
        ("2026-07-01..", (date(2026, 7, 1), None, False)),
        ("..2026-07-31", (None, date(2026, 7, 31), False)),
        ("all", (None, None, True)),
    ],
)
def test_time_range_selectors_are_inclusive_and_may_be_open(
    selector: str,
    expected: tuple[date | None, date | None, bool],
) -> None:
    options = parse_options(["--time", selector])

    assert (options.from_date, options.to_date, options.all_dates) == expected


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "2026-07-01",
        "..",
        "2026-08-01..2026-07-01",
        "2026-07-01...2026-07-31",
        "2026-W00",
        "2026-W54",
        "26-W31",
    ],
)
def test_invalid_time_selectors_are_rejected(selector: str) -> None:
    with pytest.raises(UsageError):
        parse_options(["--time", selector])


def test_time_selector_parser_defaults_to_this_week() -> None:
    assert parse_time_selectors(()) == ((), None, None, False, None)
    assert validate_iso_week("2026-W31") == "2026-W31"


@pytest.mark.parametrize(
    ("value", "expected", "label"),
    [
        ("2w", timedelta(weeks=2), "2w"),
        ("2w3d", timedelta(weeks=2, days=3), "2w3d"),
        ("6h30m", timedelta(hours=6, minutes=30), "6h30m"),
        ("1w 2d 3h 4m", timedelta(weeks=1, days=2, hours=3, minutes=4), "1w2d3h4m"),
    ],
)
def test_rolling_time_selector_uses_fixed_ordered_units(
    value: str,
    expected: timedelta,
    label: str,
) -> None:
    parsed = parse_rolling_duration(value)
    options = parse_options(["--time", value])

    assert parsed == RollingDuration(expected, label)
    assert options.rolling_duration == parsed
    assert not options.all_dates


@pytest.mark.parametrize(
    "value",
    ["0m", "-2w", "1.5d", "2d1w", "2w3w", "1h 30", "1s", "1mo", "999999999999999999999w"],
)
def test_invalid_rolling_time_selectors_are_rejected(value: str) -> None:
    with pytest.raises(UsageError, match="--time"):
        parse_options([f"--time={value}"])


def test_rolling_time_selector_cannot_be_repeated_or_mixed() -> None:
    with pytest.raises(UsageError, match="repeated only"):
        parse_options(["--time", "2w", "--time", "2026-W31"])


def test_identity_marker_style_is_opt_in() -> None:
    assert parse_options(["--marker-style", "identity"]).terminal.marker_style is MarkerStyle.IDENTITY


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("none", GridStyle.NONE),
        ("vertical", GridStyle.VERTICAL),
        ("horizontal", GridStyle.HORIZONTAL),
        ("both", GridStyle.BOTH),
    ],
)
def test_grid_style_is_explicit_and_enum_backed(value: str, expected: GridStyle) -> None:
    assert parse_options(["--grid", value]).terminal.grid_style is expected


def test_grid_style_does_not_accept_all_as_an_alias_for_both() -> None:
    with pytest.raises(SystemExit):
        parse_options(["--grid", "all"])


def test_cluster_anchor_and_band_label_are_independent_enum_options() -> None:
    options = parse_options(["--cluster-anchor", "midnight", "--band-label", "start"])

    assert options.cluster_anchor is ClusterAnchor.MIDNIGHT
    assert options.terminal.band_label is BandLabel.START


def test_show_empty_bands_requires_midnight_anchoring() -> None:
    options = parse_options(["--cluster-anchor", "midnight", "--show-empty-bands"])

    assert options.terminal.show_empty_bands

    with pytest.raises(UsageError, match="--show-empty-bands requires --cluster-anchor midnight"):
        parse_options(["--show-empty-bands"])


def test_weekday_scope_parser_accepts_aliases_groups_commas_and_repetition() -> None:
    assert parse_weekday_scopes(("Mo,wednesday", "weekend"), option="--hide-days") == (
        Weekday.MONDAY,
        Weekday.WEDNESDAY,
        Weekday.SATURDAY,
        Weekday.SUNDAY,
    )
    assert parse_options(["--hide-empty-days", "all"]).hide_empty_days == tuple(Weekday)
    assert parse_options(["--hide-days", "weekend", "--hide-days", "fri"]).hide_days == (
        Weekday.FRIDAY,
        Weekday.SATURDAY,
        Weekday.SUNDAY,
    )
    scoped_before_path = parse_options(["--hide-empty-days", "weekend", "repository"])
    assert scoped_before_path.paths == (Path("repository"),)
    short_options = parse_options(["-H", "weekend", "-E", "all"])
    assert short_options.hide_days == (Weekday.SATURDAY, Weekday.SUNDAY)
    assert short_options.hide_empty_days == tuple(Weekday)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--hide-days", ""],
        ["--hide-empty-days", "mon,"],
        ["--hide-days", "workday"],
    ],
)
def test_invalid_weekday_scopes_are_rejected(arguments: list[str]) -> None:
    with pytest.raises(UsageError):
        parse_options(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--hide-days", "all"],
        ["--hide-days", "weekdays", "--hide-days", "weekend"],
    ],
)
def test_unconditional_day_hiding_must_leave_one_column(arguments: list[str]) -> None:
    with pytest.raises(UsageError, match="all seven"):
        parse_options(arguments)


def test_mode_selects_collectors_without_changing_evidence_preset() -> None:
    filesystem = parse_options(["--mode", "fs"])
    combined = parse_options(["-m", "both"])

    assert filesystem.source is SourceMode.FILESYSTEM
    assert filesystem.profile is CollectionProfile.STANDARD
    assert combined.source is SourceMode.BOTH
    assert combined.profile is CollectionProfile.STANDARD


def test_mode_may_be_selected_only_once() -> None:
    with pytest.raises(UsageError, match="only once"):
        parse_options(["--mode", "git", "--mode", "fs"])


def test_full_git_broadens_only_git_and_preserves_explicit_time() -> None:
    options = parse_options(["-p", "full", "--time", "2026-W31"])

    assert options.source is SourceMode.GIT
    assert options.profile is CollectionProfile.FULL
    assert options.weeks == ("2026-W31",)
    assert not options.all_dates
    assert options.git_mode is GitMode.BOTH
    assert options.git_date is GitDateMode.BOTH
    assert options.git_records == GitRecords.ALL
    assert options.ref_scope is RefScope.ALL_REFS
    assert options.filesystem_entries == (FilesystemEntry.FILE,)
    assert not options.include_ignored
    assert not options.terminal.coverage


@pytest.mark.parametrize("mode", ["fs", "both"])
def test_full_filesystem_scope_includes_all_metadata_entries_and_ignored(mode: str) -> None:
    options = parse_options(["--mode", mode, "--profile", "full"])

    assert options.filesystem_times == tuple(FilesystemTime)
    assert options.filesystem_entries == tuple(FilesystemEntry)
    assert options.include_ignored
    assert not options.respect_gitignore
    assert not options.terminal.coverage
    assert not options.all_dates


def test_full_profile_keeps_expanded_coverage_opt_in() -> None:
    options = parse_options(["--profile", "full", "--coverage"])

    assert options.profile is CollectionProfile.FULL
    assert options.terminal.coverage


def test_portable_is_the_locked_git_object_backed_preset() -> None:
    options = parse_options(["--profile", "portable"])

    assert options.source is SourceMode.GIT
    assert options.profile is CollectionProfile.PORTABLE
    assert options.git_mode is GitMode.COMMITS
    assert options.git_date is GitDateMode.BOTH
    assert options.git_records == GitRecords.COMMITS | GitRecords.TAGS
    assert options.ref_scope is RefScope.ALL_REFS
    assert not options.git_records.includes_reflogs
    assert not options.terminal.coverage


@pytest.mark.parametrize("mode", ["fs", "both"])
def test_portable_rejects_non_git_modes(mode: str) -> None:
    with pytest.raises(UsageError, match="only with --mode git"):
        parse_options(["--mode", mode, "--profile", "portable"])


@pytest.mark.parametrize("profiles", [("standard", "full"), ("portable", "full"), ("full", "full")])
def test_profile_may_be_selected_only_once(profiles: tuple[str, str]) -> None:
    with pytest.raises(UsageError, match="only once"):
        parse_options(["--profile", profiles[0], "--profile", profiles[1]])


@pytest.mark.parametrize(
    "flag",
    [
        ["--git-records", "commit"],
        ["--git-commit-times", "author"],
        ["--git-commits-from", "head"],
        ["--fs-times", "modified"],
        ["--fs-entries", "file"],
        ["--respect-gitignore"],
        ["--include-ignored"],
    ],
)
@pytest.mark.parametrize("profile", ["portable", "full"])
def test_locked_profiles_reject_scope_overrides(profile: str, flag: list[str]) -> None:
    with pytest.raises(UsageError, match=f"--profile {profile} controls"):
        parse_options(["--profile", profile, *flag])


def test_explicit_standard_profile_allows_scope_customization() -> None:
    options = parse_options(
        [
            "--profile",
            "standard",
            "--git-records",
            "commit,tag",
            "--git-commit-times",
            "committer",
            "--git-commits-from",
            "all-refs",
        ]
    )

    assert options.profile is CollectionProfile.STANDARD
    assert options.git_records == GitRecords.COMMITS | GitRecords.TAGS
    assert options.git_date is GitDateMode.COMMITTER
    assert options.ref_scope is RefScope.ALL_REFS


def test_git_record_csv_consolidates_record_family_and_granularity() -> None:
    mode, records = parse_git_records("file-change,tag,reflog,file-change")

    assert mode is GitMode.FILES
    assert records == GitRecords.ALL

    options = parse_options(["--git-records", "commit,file-change,tag", "--git-commit-times", "author,committer"])
    assert options.git_mode is GitMode.BOTH
    assert options.git_date is GitDateMode.BOTH
    assert options.git_records == GitRecords.COMMITS | GitRecords.TAGS


@pytest.mark.parametrize("value", ["", "all", "commits", "file", "commit,,tag"])
def test_invalid_git_record_csv_is_rejected(value: str) -> None:
    with pytest.raises(UsageError, match="--git-records"):
        parse_options(["--git-records", value])


def test_commit_options_require_commit_derived_records() -> None:
    with pytest.raises(UsageError, match="require commit or file-change"):
        parse_options(["--git-records", "tag,reflog", "--git-commit-times", "author"])


def test_git_identity_filter_applies_to_non_commit_git_records() -> None:
    options = parse_options(
        ["--git-records", "tag,reflog", "--git-identity", "Tagger", "--git-identity", "actor@example.test"]
    )

    assert options.git_identities == ("Tagger", "actor@example.test")


def test_commits_from_maps_to_commit_reachability() -> None:
    assert parse_options(["--git-commits-from", "head"]).ref_scope is RefScope.HEAD
    assert parse_options(["--git-commits-from", "local-branches"]).ref_scope is RefScope.LOCAL_BRANCHES
    assert parse_options(["--git-commits-from", "all-refs"]).ref_scope is RefScope.ALL_REFS


def test_source_specific_options_are_not_silently_ignored() -> None:
    with pytest.raises(UsageError, match="Git-specific"):
        parse_options(["--mode", "fs", "--git-commits-from", "head"])
    with pytest.raises(UsageError, match="Git-specific"):
        parse_options(["--mode", "fs", "--git-identity", "Ada"])
    with pytest.raises(UsageError, match="filesystem-specific"):
        parse_options(["--mode", "git", "--include-ignored"])


def test_git_identity_filter_rejects_empty_values() -> None:
    with pytest.raises(UsageError, match="--git-identity values cannot be empty"):
        parse_options(["--git-identity", " "])


def test_filesystem_times_use_semantic_public_names_and_deduplicate() -> None:
    assert parse_filesystem_times("modified,birth,metadata-changed,modified") == (
        FilesystemTime.MODIFIED,
        FilesystemTime.CREATED,
        FilesystemTime.CHANGED,
    )
    with pytest.raises(UsageError):
        parse_filesystem_times("created")
    with pytest.raises(UsageError):
        parse_filesystem_times("changed")


def test_filesystem_entry_csv_is_deduplicated() -> None:
    assert parse_filesystem_entries("symlink,file,symlink") == (
        FilesystemEntry.SYMLINK,
        FilesystemEntry.FILE,
    )
    with pytest.raises(UsageError):
        parse_filesystem_entries("all")


def test_display_hours_are_half_open_and_non_overnight() -> None:
    result = parse_display_hours("06:00-22:00")

    assert result.start_minute == 360
    assert result.end_minute == 1320
    assert parse_clock_minutes("24:00", allow_24=True) == 1440
    with pytest.raises(UsageError):
        parse_display_hours("22:00-06:00")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1m", timedelta(minutes=1)),
        ("1m30s", timedelta(minutes=1, seconds=30)),
        ("10m", timedelta(minutes=10)),
        ("1h", timedelta(hours=1)),
        ("1h5m", timedelta(hours=1, minutes=5)),
        ("1h 5m", timedelta(hours=1, minutes=5)),
        ("1h\t5m 30s", timedelta(hours=1, minutes=5, seconds=30)),
    ],
)
def test_cluster_window_accepts_compact_ordered_durations(value: str, expected: timedelta) -> None:
    assert parse_cluster_window(value) == expected
    assert parse_options(["--cluster-window", value]).cluster_window == expected


def test_midnight_anchor_requires_a_whole_minute_cluster_window() -> None:
    with pytest.raises(UsageError, match="midnight.*whole minutes.*HH:MM"):
        parse_options(["--cluster-anchor", "midnight", "--cluster-window", "1m30s"])

    options = parse_options(["--cluster-anchor", "midnight", "--cluster-window", "1h5m"])
    assert options.cluster_window == timedelta(hours=1, minutes=5)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "10",
        "0s",
        "30s",
        "59s",
        "-10m",
        "1.5h",
        "1d",
        "5m1h",
        "1h2h",
        "1h 5m 2m",
        "1 h",
        "24h",
        "23h60m",
    ],
)
def test_cluster_window_rejects_subminute_invalid_or_day_sized_durations(value: str) -> None:
    with pytest.raises(UsageError, match="--cluster-window"):
        parse_cluster_window(value)


def test_limit_is_only_valid_for_the_outside_list() -> None:
    with pytest.raises(UsageError, match="--limit requires"):
        parse_options(["--limit", "10"])
    with pytest.raises(UsageError, match="at least 1"):
        parse_options(["--list-outside", "--limit", "0"])


def test_explicit_exclusions_cannot_be_negated() -> None:
    with pytest.raises(UsageError, match="cannot be negated"):
        parse_options(["--mode", "fs", "--exclude", "!keep.log"])
