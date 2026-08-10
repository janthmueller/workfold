from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from workfold.cli import parse_options
from workfold.config import (
    CollectionProfile,
    FilesystemEntry,
    FilesystemTime,
    GitDateMode,
    GitMode,
    GitRecords,
    RefScope,
    SourceMode,
    UsageError,
    parse_clock_minutes,
    parse_cluster_window,
    parse_display_hours,
    parse_filesystem_entries,
    parse_filesystem_times,
    parse_git_records,
    parse_time_selectors,
    validate_iso_week,
)


def test_default_options_are_the_quick_git_view_for_this_week() -> None:
    options = parse_options([])

    assert options.paths == (Path("."),)
    assert options.source is SourceMode.GIT
    assert options.weeks == ()
    assert options.from_date is None
    assert options.to_date is None
    assert not options.all_dates
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


def test_explicit_this_week_matches_the_implicit_default() -> None:
    implicit = parse_options([])
    explicit = parse_options(["--time", "this-week"])

    assert explicit.weeks == implicit.weeks
    assert explicit.from_date == implicit.from_date
    assert explicit.to_date == implicit.to_date
    assert explicit.all_dates == implicit.all_dates


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
    assert parse_time_selectors(()) == ((), None, None, False)
    assert validate_iso_week("2026-W31") == "2026-W31"


def test_mode_selects_collectors_without_changing_collection_depth() -> None:
    filesystem = parse_options(["--mode", "fs"])
    combined = parse_options(["-m", "all"])

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
    assert not options.coverage


@pytest.mark.parametrize("mode", ["fs", "all"])
def test_full_filesystem_scope_includes_all_metadata_entries_and_ignored(mode: str) -> None:
    options = parse_options(["--mode", mode, "--profile", "full"])

    assert options.filesystem_times == tuple(FilesystemTime)
    assert options.filesystem_entries == tuple(FilesystemEntry)
    assert options.include_ignored
    assert not options.respect_gitignore
    assert not options.coverage
    assert not options.all_dates


def test_full_profile_keeps_expanded_coverage_opt_in() -> None:
    options = parse_options(["--profile", "full", "--coverage"])

    assert options.profile is CollectionProfile.FULL
    assert options.coverage


def test_portable_is_the_locked_git_object_backed_preset() -> None:
    options = parse_options(["--profile", "portable"])

    assert options.source is SourceMode.GIT
    assert options.profile is CollectionProfile.PORTABLE
    assert options.git_mode is GitMode.COMMITS
    assert options.git_date is GitDateMode.BOTH
    assert options.git_records == GitRecords.COMMITS | GitRecords.TAGS
    assert options.ref_scope is RefScope.ALL_REFS
    assert not options.git_records.includes_reflogs
    assert not options.coverage


@pytest.mark.parametrize("mode", ["fs", "all"])
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
        ("30s", timedelta(seconds=30)),
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


@pytest.mark.parametrize(
    "value",
    ["", "10", "0s", "-10m", "1.5h", "1d", "5m1h", "1h2h", "1h 5m 2m", "1 h", "24h", "23h60m"],
)
def test_cluster_window_rejects_invalid_or_day_sized_durations(value: str) -> None:
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
