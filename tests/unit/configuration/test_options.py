from __future__ import annotations

from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pytest
from workfold.cli import parse_options
from workfold.configuration import (
    BandLabel,
    ClusterAnchor,
    CollectionProfile,
    EventListSelection,
    GridStyle,
    ListSchedule,
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
    parse_event_list,
    parse_event_selectors,
    parse_rolling_duration,
    parse_time_selectors,
    parse_weekday_scopes,
    validate_iso_week,
)
from workfold.domain.evidence import EvidenceKind, EvidenceSelection
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
    assert options.evidence == EvidenceSelection.create((EvidenceKind.GIT_COMMIT_AUTHOR,))
    assert options.ref_scope is RefScope.LOCAL_BRANCHES
    assert options.git_identities == ()
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
        "event_list",
        "event_limit",
        "coverage",
        "strict",
        "verbose",
        "event_styles",
    )
    assert unresolved_names[-6:] == (
        "strict",
        "verbose",
        "explicit_names",
        "cluster_anchor",
        "band_label",
        "show_empty_bands",
    )
    assert resolved.terminal.event_limit == 50


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
    assert options.evidence == EvidenceSelection.create(kind for kind in EvidenceKind if kind.value.startswith("git:"))
    assert options.ref_scope is RefScope.ALL_REFS
    assert not options.include_ignored
    assert not options.terminal.coverage


@pytest.mark.parametrize("mode", ["fs", "both"])
def test_full_filesystem_scope_includes_all_metadata_entries_and_ignored(mode: str) -> None:
    options = parse_options(["--mode", mode, "--profile", "full"])

    assert all(kind in options.evidence.kinds for kind in EvidenceKind if kind.value.startswith("fs:"))
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
    assert options.evidence == EvidenceSelection.create(
        (
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.GIT_COMMIT_COMMITTER,
            EvidenceKind.GIT_TAG_TAGGER,
        )
    )
    assert options.ref_scope is RefScope.ALL_REFS
    assert EvidenceKind.GIT_REFLOG_UPDATE not in options.evidence.kinds
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
        ["--git-commits-from", "head"],
        ["--respect-gitignore"],
        ["--include-ignored"],
    ],
)
@pytest.mark.parametrize("profile", ["portable", "full"])
def test_locked_profiles_reject_scope_overrides(profile: str, flag: list[str]) -> None:
    with pytest.raises(UsageError, match=f"--profile {profile} controls"):
        parse_options(["--profile", profile, *flag])


def test_custom_events_are_an_exact_alternative_to_mode_and_profile() -> None:
    options = parse_options(
        [
            "--events",
            "git:commit:committer",
            "git:tag:tagger",
            "--git-commits-from",
            "all-refs",
        ]
    )

    assert options.profile is CollectionProfile.CUSTOM
    assert options.evidence == EvidenceSelection.create(
        (EvidenceKind.GIT_COMMIT_COMMITTER, EvidenceKind.GIT_TAG_TAGGER)
    )
    assert options.ref_scope is RefScope.ALL_REFS


def test_event_selectors_expand_wildcards_deduplicate_and_keep_canonical_order() -> None:
    selection = parse_event_selectors(("git:*:committer", "git:tag:tagger", "git:commit:committer"))

    assert selection.kinds == (
        EvidenceKind.GIT_COMMIT_COMMITTER,
        EvidenceKind.GIT_FILE_CHANGE_COMMITTER,
        EvidenceKind.GIT_TAG_TAGGER,
    )
    expanded = parse_options(["--events", "fs:*", "git:tag:tagger"]).evidence
    assert EvidenceKind.GIT_TAG_TAGGER in expanded.kinds
    assert tuple(kind for kind in expanded.kinds if kind.source.value == "filesystem") == tuple(
        kind for kind in EvidenceKind if kind.source.value == "filesystem"
    )


def test_short_event_and_list_options_keep_multi_value_semantics() -> None:
    options = parse_options(
        ["-e", "git:tag:tagger", "fs:file:modified", "-l", "outside", "git:tag:tagger"],
    )

    assert options.evidence == EvidenceSelection.create(
        (EvidenceKind.GIT_TAG_TAGGER, EvidenceKind.FS_FILE_MODIFIED),
    )
    assert options.terminal.event_list == EventListSelection(
        schedule=ListSchedule.OUTSIDE,
        evidence_kinds=(EvidenceKind.GIT_TAG_TAGGER,),
    )


@pytest.mark.parametrize("value", ["", "git", "git:commit", "git:commits:*", "fs:created", "git:*"])
def test_invalid_or_unexpanded_event_selectors_are_rejected(value: str) -> None:
    arguments = ["--events", value]
    if value == "git:*":
        arguments = ["--events", "git:*", "--events", "unknown"]
    with pytest.raises(UsageError, match="--events"):
        parse_options(arguments)


def test_commit_reachability_requires_commit_derived_events() -> None:
    with pytest.raises(UsageError, match="requires commit or file-change"):
        parse_options(["--events", "git:tag:tagger", "git:reflog:update", "--git-commits-from", "head"])


@pytest.mark.parametrize("preset_flag", [("--mode", "git"), ("--profile", "standard")])
def test_custom_events_cannot_share_a_cli_layer_with_presets(preset_flag: tuple[str, str]) -> None:
    with pytest.raises(UsageError, match="--events cannot be combined"):
        parse_options(["--events", "git:tag:tagger", *preset_flag])


def test_git_identity_filter_applies_to_non_commit_git_records() -> None:
    options = parse_options(
        [
            "--events",
            "git:tag:tagger",
            "git:reflog:update",
            "--git-identity",
            "Tagger",
            "--git-identity",
            "actor@example.test",
        ]
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
    with pytest.raises(UsageError, match="filesystem-specific"):
        parse_options(["--mode", "git", "--fs-exclude", "*.log"])

    options = parse_options(["--mode", "fs", "--fs-exclude", "build", "--fs-exclude", "*.log"])
    assert options.exclusions == ("build", "*.log")


def test_git_identity_filter_rejects_empty_values() -> None:
    with pytest.raises(UsageError, match="--git-identity values cannot be empty"):
        parse_options(["--git-identity", " "])


def test_filesystem_event_selectors_include_entry_type_and_expand_each_axis() -> None:
    assert parse_event_selectors(("fs:file:modified", "fs:directory:birth", "fs:symlink:metadata-changed")).kinds == (
        EvidenceKind.FS_FILE_MODIFIED,
        EvidenceKind.FS_DIRECTORY_BIRTH,
        EvidenceKind.FS_SYMLINK_METADATA_CHANGED,
    )
    assert parse_event_selectors(("fs:*:modified",)).kinds == (
        EvidenceKind.FS_FILE_MODIFIED,
        EvidenceKind.FS_DIRECTORY_MODIFIED,
        EvidenceKind.FS_SYMLINK_MODIFIED,
    )
    with pytest.raises(UsageError, match="fs:file:birth"):
        parse_event_selectors(("fs:created",))


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


def test_general_event_list_supports_schedule_and_event_intersection() -> None:
    options = parse_options(
        ["--events", "git:*", "--list", "outside", "git:tag:tagger", "git:commit:*", "--limit", "7"]
    )

    assert options.terminal.event_list == EventListSelection(
        schedule=ListSchedule.OUTSIDE,
        evidence_kinds=(
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.GIT_COMMIT_COMMITTER,
            EvidenceKind.GIT_TAG_TAGGER,
        ),
    )
    assert options.terminal.event_limit == 7
    assert parse_event_list(("all",)) == EventListSelection()
    assert parse_event_list(("none",)) is None

    explicit_all = parse_options(
        ["--events", "git:tag:tagger", "--list", "all", "git:tag:tagger"],
    )
    assert explicit_all.terminal.event_list == EventListSelection(
        schedule=ListSchedule.ALL,
        evidence_kinds=(EvidenceKind.GIT_TAG_TAGGER,),
    )


def test_event_list_selection_rejects_non_enum_evidence_values() -> None:
    raw = cast(tuple[EvidenceKind, ...], ("git:tag:tagger",))

    with pytest.raises(TypeError, match="EvidenceKind"):
        EventListSelection(evidence_kinds=raw)


def test_limit_is_only_valid_for_an_active_event_list() -> None:
    with pytest.raises(UsageError, match="--limit requires"):
        parse_options(["--limit", "10"])
    with pytest.raises(UsageError, match="at least 1"):
        parse_options(["--list", "outside", "--limit", "0"])


@pytest.mark.parametrize(
    "selectors",
    [
        ("all", "outside"),
        ("none", "git:tag:tagger"),
        ("inside", "outside"),
        ("outside", "outside"),
    ],
)
def test_invalid_event_list_predicates_are_rejected(selectors: tuple[str, ...]) -> None:
    with pytest.raises(UsageError, match="--list"):
        parse_event_list(selectors)


@pytest.mark.parametrize(
    ("enabled", "listed", "missing"),
    [
        (("git:tag:tagger",), ("fs:file:modified",), "fs:file:modified"),
        (("git:tag:tagger",), ("git:tag:tagger", "fs:file:modified"), "fs:file:modified"),
    ],
)
def test_every_list_event_selector_must_be_enabled(
    enabled: tuple[str, ...],
    listed: tuple[str, ...],
    missing: str,
) -> None:
    with pytest.raises(UsageError, match=rf"{missing}.*matches no event kind enabled"):
        parse_options(["--events", *enabled, "--list", *listed])


def test_list_wildcards_match_only_enabled_event_kinds() -> None:
    options = parse_options(
        ["--events", "git:tag:tagger", "fs:file:modified", "--list", "git:*"],
    )

    assert options.terminal.event_list == EventListSelection(
        evidence_kinds=(EvidenceKind.GIT_TAG_TAGGER,),
    )

    with pytest.raises(UsageError, match=r"git:\*.*matches no event kind enabled"):
        parse_options(["--events", "fs:file:modified", "--list", "git:*"])


def test_explicit_exclusions_cannot_be_negated() -> None:
    with pytest.raises(UsageError, match="cannot be negated"):
        parse_options(["--mode", "fs", "--fs-exclude", "!keep.log"])
