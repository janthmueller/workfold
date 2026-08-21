from __future__ import annotations

import re
from datetime import timedelta

import pytest
from wuf.configuration import (
    BandLabel,
    ClusterAnchor,
    CountGrouping,
    GridStyle,
    MarkerStyle,
    compile_event_style_sheet,
    parse_event_style_rules,
)
from wuf.domain.observations import Source, TimestampKind, Weekday
from wuf.reporting.sanitization import display_width
from wuf.reporting.terminal import TerminalOptions, render_terminal
from wuf.reporting.terminal.coverage_status import COMPLETE_COVERAGE_STATUS

from support.reports import classified_marker as _classified
from support.reports import coalesced_git_marker as _coalesced_git
from support.reports import report as _report


def test_plain_renderer_uses_event_symbols_for_mixed_and_outside_activity() -> None:
    report = _report(
        _classified("abcdef012345", 8, 5, source=Source.GIT, within_schedule=True),
        _classified("note.txt", 8, 9, source=Source.FILESYSTEM, within_schedule=False),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))

    assert rendered.startswith("Time band")
    assert rendered.index("Working hours") < rendered.index("Events")
    assert "Legend" not in rendered
    assert "Summary" not in rendered
    assert "Wuf ·" not in rendered
    assert "08:05–08:09" in rendered
    assert "●□" in rendered
    assert "G=" not in rendered and "F=" not in rendered and "M=" not in rendered
    assert "Density:" not in rendered
    assert "Events    2" in rendered
    assert "Schedule  1 inside (50.0%) · 1 outside (50.0%)" in rendered
    assert "Calendar  2 weekday (100.0%) · 0 weekend (0.0%)" in rendered
    assert "● Git · ■ Filesystem · □ Outside working hours" in rendered
    assert "Working hours: Mo-Fr 08:00-16:30" in rendered
    assert "×N exact per event kind" not in rendered
    assert "Breakdown" not in rendered
    assert "Scope" not in rendered
    assert "Period" not in rendered
    assert COMPLETE_COVERAGE_STATUS not in rendered
    assert "Cluster window:" not in rendered
    assert "empty time omitted" not in rendered
    assert "Collector selectors:" not in rendered
    assert "Git commits + filesystem metadata" not in rendered
    assert "Git identities:" not in rendered
    assert "Filesystem policy:" not in rendered
    assert "\x1b[" not in rendered
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_colored_renderer_colors_sources_and_outside_events_individually() -> None:
    report = _report(
        _classified("inside", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("filesystem", 9, 0, source=Source.FILESYSTEM, within_schedule=True),
        _classified("outside", 18, 0, source=Source.GIT, within_schedule=False),
    )

    rendered = render_terminal(report, options=TerminalOptions(color=True))

    assert "\x1b[32m●\x1b[0m" in rendered
    assert "\x1b[94m■\x1b[0m" in rendered
    assert "\x1b[1;91m○\x1b[0m" in rendered
    assert rendered.count("○") == 2
    assert "\x1b[1;91m○ Outside working hours\x1b[0m" in rendered


def test_source_markers_and_legend_use_configured_event_symbols_and_colors() -> None:
    report = _report(
        _classified("inside", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("outside", 18, 0, source=Source.GIT, within_schedule=False),
        _classified("filesystem", 9, 0, source=Source.FILESYSTEM, within_schedule=True),
    )
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {
                    "git:commit:author": {
                        "symbol": "C",
                        "color": "magenta",
                        "outside-symbol": "c",
                        "outside-color": "yellow",
                    },
                    "fs:file:modified": {"symbol": "M", "color": "cyan"},
                },
                location="styles",
            ),
        )
    )

    plain = render_terminal(report, options=TerminalOptions(width=80, color=False, event_styles=styles))
    colored = render_terminal(report, options=TerminalOptions(width=80, color=True, event_styles=styles))

    assert re.search(r"^08:00\s+C", plain, re.MULTILINE)
    assert re.search(r"^09:00\s+M", plain, re.MULTILINE)
    assert re.search(r"^18:00\s+c", plain, re.MULTILINE)
    assert "C Git commits · M Filesystem files · c Outside working hours" in plain
    assert "\x1b[35mC\x1b[0m" in colored
    assert "\x1b[36mM\x1b[0m" in colored
    assert "\x1b[1;33mc\x1b[0m" in colored


def test_legend_shows_each_visible_custom_outside_color() -> None:
    report = _report(
        _classified("git-outside", 18, 0, source=Source.GIT, within_schedule=False),
        _classified("filesystem-outside", 18, 1, source=Source.FILESYSTEM, within_schedule=False),
    )
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {
                    "git:commit:author": {"outside-symbol": "g", "outside-color": "yellow"},
                    "fs:file:modified": {"outside-symbol": "f", "outside-color": "magenta"},
                },
                location="styles",
            ),
        )
    )

    plain = render_terminal(report, options=TerminalOptions(width=80, color=False, event_styles=styles))
    colored = render_terminal(report, options=TerminalOptions(width=80, color=True, event_styles=styles))

    assert "● Git commits" in plain
    assert "g/f Outside working hours" in plain
    assert "\x1b[1;33mg\x1b[0m" in colored
    assert "\x1b[1;35mf\x1b[0m" in colored


def test_invisible_custom_style_does_not_make_the_visible_default_legend_more_specific() -> None:
    report = _report(_classified("commit", 9, 0, source=Source.GIT, within_schedule=True))
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {"git:tag:*": {"symbol": "T"}},
                location="styles",
            ),
        )
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False, event_styles=styles))

    assert "● Git" in rendered
    assert "Git commits" not in rendered and "Git tags" not in rendered


def test_coalesced_git_marker_uses_a_style_selector_covering_both_roles() -> None:
    report = _report(_coalesced_git("same", 9, within_schedule=True))
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {
                    "git:commit:author": {"symbol": "A"},
                    "git:commit:*": {"symbol": "C"},
                },
                location="styles",
            ),
        )
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False, event_styles=styles))

    assert "C" in rendered
    assert "A" not in rendered


def test_identity_markers_keep_identity_symbols_and_schedule_colors_with_custom_event_styles() -> None:
    report = _report(
        _classified(
            "inside",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        _classified(
            "outside",
            18,
            0,
            source=Source.GIT,
            within_schedule=False,
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        retain_git_identities=True,
    )
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {
                    "git:*": {
                        "symbol": "X",
                        "color": "magenta",
                        "outside-symbol": "x",
                        "outside-color": "yellow",
                    }
                },
                location="styles",
            ),
        )
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            color=True,
            marker_style=MarkerStyle.IDENTITY,
            event_styles=styles,
        ),
    )

    assert "\x1b[35mX\x1b[0m" not in rendered
    assert "\x1b[1;33mx\x1b[0m" not in rendered
    assert "\x1b[32mA\x1b[0m" in rendered
    assert "\x1b[1;91ma\x1b[0m" in rendered


def test_identity_markers_are_collision_free_mapped_and_schedule_aware_without_color() -> None:
    report = _report(
        _classified(
            "ada-inside",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Ada Person",
            actor_email="ada@example.test",
        ),
        _classified(
            "alice-inside",
            8,
            1,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Alice Person",
            actor_email="alice@example.test",
        ),
        _classified(
            "ada-outside",
            18,
            0,
            source=Source.GIT,
            within_schedule=False,
            actor_name="Ada Person",
            actor_email="ada@example.test",
        ),
        _classified("fs-outside", 18, 1, source=Source.FILESYSTEM, within_schedule=False),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=100, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert "A1A2" in rendered
    assert "a1□" in rendered
    assert "A1 Ada Person <ada@example.test>" in rendered
    assert "A2 Alice Person <alice@example.test>" in rendered
    assert "a–z/□ Outside working hours" in rendered
    assert "A/a" not in rendered
    assert "●" not in rendered and "○" not in rendered


def test_identity_markers_use_case_and_color_for_schedule_status() -> None:
    report = _report(
        _classified(
            "inside",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        _classified(
            "outside",
            18,
            0,
            source=Source.GIT,
            within_schedule=False,
            actor_name="Ada",
            actor_email="ada@example.test",
        ),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, color=True, marker_style=MarkerStyle.IDENTITY),
    )

    assert "\x1b[32mA\x1b[0m" in rendered
    assert "\x1b[1;91ma\x1b[0m" in rendered
    assert "\x1b[32mA\x1b[0m Ada <ada@example.test>" in rendered
    assert rendered.count("Ada <ada@example.test>") == 1
    assert "\x1b[1;91ma–z Outside working hours\x1b[0m" in rendered


def test_identity_legend_sanitizes_recorded_git_text() -> None:
    report = _report(
        _classified(
            "hostile",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Ada\n\x1b[31mforged",
            actor_email="ada@example.test",
        ),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=100, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert r"Ada\n\x1b[31mforged <ada@example.test>" in rendered
    assert "\x1b[31mforged" not in rendered.replace(r"\x1b[31mforged", "")


def test_long_identity_legend_wraps_without_losing_the_recorded_value() -> None:
    long_name = "A" * 120
    report = _report(
        _classified(
            "long",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name=long_name,
            actor_email="long@example.test",
        ),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert long_name in rendered.replace("\n", "")
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_identity_mode_leaves_filesystem_symbols_unchanged() -> None:
    report = _report(
        _classified("inside", 8, 0, source=Source.FILESYSTEM, within_schedule=True),
        _classified("outside", 18, 0, source=Source.FILESYSTEM, within_schedule=False),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert "■" in rendered and "□" in rendered
    assert "■ Filesystem · □ Outside working hours" in rendered


def test_identity_mode_falls_back_cleanly_when_the_registry_limit_is_exceeded() -> None:
    report = _report(
        _classified("ada", 8, 0, source=Source.GIT, within_schedule=True, actor_name="Ada"),
        _classified("bob", 8, 1, source=Source.GIT, within_schedule=True, actor_name="Bob"),
        retain_git_identities=True,
        identity_limit=1,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=100, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert "●●" in rendered
    assert "● Git" in rendered
    assert "identity limit exceeded" in rendered
    assert "Ada" not in rendered and "Bob" not in rendered


def test_coalesced_different_identities_use_filled_and_hollow_diamonds() -> None:
    report = _report(
        _coalesced_git("inside", 8, within_schedule=True),
        _coalesced_git("outside", 18, within_schedule=False),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=100, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert "◆ Ada Author <ada@example.test> & Bob Committer <bob@example.test>" in rendered
    assert "◇ Outside working hours" in rendered

    colored = render_terminal(
        report,
        options=TerminalOptions(width=100, color=True, marker_style=MarkerStyle.IDENTITY),
    )

    assert "\x1b[32m◆\x1b[0m" in colored
    assert "\x1b[1;91m◇\x1b[0m" in colored
    assert "\x1b[32m◆\x1b[0m Ada Author <ada@example.test>" in colored
    assert "\x1b[1;91m◇ Outside" in colored


def test_legend_groups_all_outside_symbols_without_repeating_source_names() -> None:
    report = _report(
        _classified("inside-fs", 8, 0, source=Source.FILESYSTEM, within_schedule=True),
        _classified("outside-fs", 18, 0, source=Source.FILESYSTEM, within_schedule=False),
        _classified("outside-git", 18, 1, source=Source.GIT, within_schedule=False),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))

    assert "● Git · ■ Filesystem · ○/□ Outside working hours" in rendered
    assert "Git outside working hours" not in rendered
    assert "Filesystem outside working hours" not in rendered


def test_chart_omits_empty_rows_and_marks_large_compressed_gaps() -> None:
    report = _report(
        _classified("first", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("second", 10, 0, source=Source.GIT, within_schedule=True),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))

    assert "08:00" in rendered and "10:00" in rendered
    assert "⋮ 2h" in rendered
    assert "08:30" not in rendered and "09:00" not in rendered and "09:30" not in rendered


def test_gap_cue_threshold_tracks_the_event_cluster_window() -> None:
    below_threshold = _report(
        _classified("anchor", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("last-in-band", 8, 8, source=Source.GIT, within_schedule=True),
        _classified("next", 8, 17, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(minutes=10),
    )
    at_threshold = _report(
        _classified("anchor", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("last-in-band", 8, 8, source=Source.GIT, within_schedule=True),
        _classified("next", 8, 18, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(minutes=10),
    )

    below = render_terminal(below_threshold, options=TerminalOptions(width=80))
    at = render_terminal(at_threshold, options=TerminalOptions(width=80))

    assert "⋮" not in below
    assert "⋮ 10m" in at


def test_midnight_gap_cue_marks_one_omitted_fixed_band() -> None:
    report = _report(
        _classified("first", 8, 1, source=Source.GIT, within_schedule=True),
        _classified("second", 8, 21, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(minutes=10),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))

    assert "08:00–08:10" in rendered
    assert "08:20–08:30" in rendered
    assert "⋮ 10m" in rendered


def test_second_based_event_window_uses_an_exact_second_gap_cue() -> None:
    report = _report(
        _classified("first", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("second", 8, 1, second=30, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(minutes=1, seconds=30),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))

    assert "⋮ 1m 30s" in rendered


def test_subsecond_programmatic_window_preserves_fractional_gap_precision() -> None:
    report = _report(
        _classified("first", 8, 0, source=Source.GIT, within_schedule=True),
        _classified(
            "second",
            8,
            1,
            second=30,
            fractional_nanoseconds=500_000_000,
            source=Source.GIT,
            within_schedule=True,
        ),
        cluster_window=timedelta(minutes=1, seconds=30, microseconds=500_000),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))

    assert "⋮ 1m 30.5s" in rendered


def test_whole_minute_window_omits_residual_gap_seconds() -> None:
    report = _report(
        _classified("first", 8, 0, second=15, source=Source.GIT, within_schedule=True),
        _classified("second", 10, 30, second=45, source=Source.GIT, within_schedule=True),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, grid_style=GridStyle.BOTH),
    )

    assert "⋮ 2h 30m" in rendered
    assert "⋮ 2h 30m 30s" not in rendered
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


@pytest.mark.parametrize(
    ("cluster_anchor", "band_label", "heading", "row_label"),
    [
        (ClusterAnchor.EVENT, BandLabel.RANGE, "Time band", "10:14–10:57"),
        (ClusterAnchor.EVENT, BandLabel.START, "Time", "10:14"),
        (ClusterAnchor.MIDNIGHT, BandLabel.RANGE, "Time band", "10:00–11:00"),
        (ClusterAnchor.MIDNIGHT, BandLabel.START, "Time", "10:00"),
    ],
)
def test_cluster_anchor_and_band_label_are_independent_rendering_controls(
    cluster_anchor: ClusterAnchor,
    band_label: BandLabel,
    heading: str,
    row_label: str,
) -> None:
    report = _report(
        _classified("first", 10, 14, source=Source.GIT, within_schedule=True),
        _classified("last", 10, 57, source=Source.GIT, within_schedule=True),
        cluster_anchor=cluster_anchor,
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, band_label=band_label))

    assert rendered.startswith(heading)
    assert re.search(rf"^{re.escape(row_label)}(?:\s|│)", rendered, re.MULTILINE)


def test_midnight_clusters_report_only_wholly_omitted_interval_time_as_a_gap() -> None:
    report = _report(
        _classified("first", 8, 59, source=Source.GIT, within_schedule=True),
        _classified("second", 12, 1, source=Source.GIT, within_schedule=True),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))

    assert "⋮ 3h" in rendered
    assert "⋮ 3h 2m" not in rendered


def test_show_empty_bands_expands_the_resolved_midnight_range() -> None:
    report = _report(
        _classified("first", 8, 5, source=Source.GIT, within_schedule=True),
        _classified("second", 10, 5, source=Source.GIT, within_schedule=True),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60, 11 * 60),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, show_empty_bands=True),
    )

    assert re.search(r"^08:00–09:00\s+\u25cf", rendered, re.MULTILINE)
    assert re.search(r"^09:00–10:00\s*$", rendered, re.MULTILINE)
    assert re.search(r"^10:00–11:00\s+\u25cf", rendered, re.MULTILINE)
    assert "⋮" not in rendered


def test_show_empty_bands_clips_edge_labels_to_an_explicit_display_crop() -> None:
    report = _report(
        _classified("event", 8, 45, source=Source.GIT, within_schedule=True),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60 + 30, 10 * 60 + 15),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, show_empty_bands=True),
    )

    assert re.search(r"^08:30–09:00\s+\u25cf", rendered, re.MULTILINE)
    assert re.search(r"^09:00–10:00\s*$", rendered, re.MULTILINE)
    assert re.search(r"^10:00–10:15\s*$", rendered, re.MULTILINE)


def test_dense_start_labels_use_exact_ranges_for_explicitly_clipped_edges() -> None:
    report = _report(
        _classified("event", 8, 45, source=Source.GIT, within_schedule=True),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60 + 30, 10 * 60 + 15),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            band_label=BandLabel.START,
            show_empty_bands=True,
        ),
    )

    assert re.search(r"^08:30–09:00\s+\u25cf", rendered, re.MULTILINE)
    assert re.search(r"^09:00\s*$", rendered, re.MULTILINE)
    assert re.search(r"^10:00–10:15\s*$", rendered, re.MULTILINE)


def test_dense_automatic_range_expands_to_complete_midnight_bands() -> None:
    report = _report(
        _classified("event", 8, 45, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(hours=1, minutes=5),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        schedule_bounds=(8 * 60, 16 * 60 + 30),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            band_label=BandLabel.START,
            show_empty_bands=True,
        ),
    )

    assert re.search(r"^07:35\s*$", rendered, re.MULTILINE)
    assert re.search(r"^08:40\s+\u25cf", rendered, re.MULTILINE)
    assert re.search(r"^16:15\s*$", rendered, re.MULTILINE)
    assert not re.search(r"^08:00(?:\s|$)", rendered, re.MULTILINE)


def test_show_empty_bands_still_renders_a_range_without_events() -> None:
    report = _report(
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60, 10 * 60),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, show_empty_bands=True),
    )

    assert re.search(r"^08:00–09:00\s*$", rendered, re.MULTILINE)
    assert re.search(r"^09:00–10:00\s*$", rendered, re.MULTILINE)
    assert "No events in selected scope." not in rendered


def test_show_empty_bands_rejects_event_anchored_reports() -> None:
    report = _report(_classified("event", 8, 0, source=Source.GIT, within_schedule=True))

    with pytest.raises(ValueError, match="requires midnight-anchored"):
        render_terminal(report, options=TerminalOptions(width=80, show_empty_bands=True))


@pytest.mark.parametrize(
    ("grid_style", "vertical", "horizontal"),
    [
        (GridStyle.NONE, False, False),
        (GridStyle.VERTICAL, True, False),
        (GridStyle.HORIZONTAL, False, True),
        (GridStyle.BOTH, True, True),
    ],
)
def test_grid_styles_render_only_the_requested_internal_lines(
    grid_style: GridStyle,
    vertical: bool,
    horizontal: bool,
) -> None:
    report = _report(
        _classified("monday", 8, 0, source=Source.GIT, within_schedule=True),
        hide_empty_days=tuple(Weekday),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, grid_style=grid_style))
    chart_lines = rendered.split("\n\n", maxsplit=1)[0].splitlines()

    assert ("│" in chart_lines[0]) is vertical
    assert any("─" in line for line in chart_lines) is horizontal
    assert any("┼" in line for line in chart_lines) is (vertical and horizontal)
    assert not chart_lines[0].startswith("│") and not chart_lines[0].endswith("│")
    assert all(display_width(line) <= 80 for line in chart_lines)


def test_dense_horizontal_grid_separates_bands_but_not_wrapped_event_lines() -> None:
    markers = tuple(
        _classified(f"busy-{index}", 8, 5, source=Source.GIT, within_schedule=True) for index in range(20)
    ) + (_classified("later", 10, 5, source=Source.GIT, within_schedule=True),)
    report = _report(
        *markers,
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60, 11 * 60),
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            grid_style=GridStyle.BOTH,
            show_empty_bands=True,
        ),
    )
    chart_lines = rendered.split("\n\n", maxsplit=1)[0].splitlines()
    boundaries = [line for line in chart_lines if "┼" in line]

    assert "↳" in rendered
    assert len(boundaries) == 3  # Header plus one separator between each fixed band.
    assert all(boundary.count("┼") == 7 for boundary in boundaries)
    assert "⋮" not in rendered
    assert all(display_width(line) <= 80 for line in chart_lines)


def test_horizontal_grid_separates_clusters_but_not_wrapped_rows_or_compressed_gaps() -> None:
    markers = tuple(
        _classified(f"busy-{index}", 8, 0, source=Source.GIT, within_schedule=True) for index in range(25)
    ) + (
        _classified("nearby", 8, 30, source=Source.GIT, within_schedule=True),
        _classified("later", 10, 30, source=Source.GIT, within_schedule=True),
    )
    report = _report(*markers, cluster_window=timedelta(minutes=10))

    rendered = render_terminal(report, options=TerminalOptions(width=80, grid_style=GridStyle.BOTH))
    chart_lines = rendered.split("\n\n", maxsplit=1)[0].splitlines()
    boundaries = [line for line in chart_lines if "┼" in line]
    gap_boundaries = [line for line in boundaries if "⋮ 2h" in line]

    assert len(boundaries) == 3  # Header plus one boundary for each pair of clusters.
    assert "↳" in rendered
    assert len(gap_boundaries) == 1
    assert all(boundary.count("┼") == 7 for boundary in boundaries)
    assert all("┼" in line for line in chart_lines if "⋮" in line)


def test_minimum_cluster_window_keeps_minute_only_row_labels_unique() -> None:
    report = _report(
        _classified("first", 9, 0, second=5, source=Source.GIT, within_schedule=True),
        _classified("same-minute", 9, 0, second=45, source=Source.GIT, within_schedule=True),
        _classified("next", 9, 1, second=45, source=Source.GIT, within_schedule=True),
        cluster_window=timedelta(minutes=1),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80))
    labels = [line.split(maxsplit=1)[0] for line in rendered.splitlines() if re.match(r"^[0-9]{2}:[0-9]{2}", line)]

    assert labels == ["09:00", "09:01"]


def test_empty_result_keeps_matrix_header_before_empty_state_and_summary() -> None:
    rendered = render_terminal(_report(), options=TerminalOptions(width=80))

    assert rendered.startswith("Time band")
    assert rendered.index("No events in selected scope.") < rendered.index("Working hours")
    assert rendered.index("Working hours") < rendered.index("Events")
    assert "Legend" not in rendered
    assert "Summary" not in rendered


def test_busy_cells_compact_to_exact_symbol_counts() -> None:
    markers = tuple(
        _classified(f"git-{index}", 8, 0, source=Source.GIT, within_schedule=True) for index in range(25)
    ) + (_classified("outside-fs", 8, 1, source=Source.FILESYSTEM, within_schedule=False),)

    rendered = render_terminal(_report(*markers), options=TerminalOptions(width=80))

    assert "●×25" in rendered
    assert "□×1" in rendered
    assert "×N exact per visual" in rendered
    assert "Events    26" in rendered


def test_busy_count_tokens_can_group_matching_event_kinds_by_resolved_visual() -> None:
    markers = tuple(
        _classified(
            f"modified-{index}",
            8,
            0,
            source=Source.FILESYSTEM,
            within_schedule=True,
            timestamp_kind=TimestampKind.FS_MODIFIED,
        )
        for index in range(15)
    ) + tuple(
        _classified(
            f"birth-{index}",
            8,
            0,
            source=Source.FILESYSTEM,
            within_schedule=True,
            timestamp_kind=TimestampKind.FS_CREATED,
        )
        for index in range(15)
    )
    report = _report(*markers)

    per_visual = render_terminal(report, options=TerminalOptions(width=80))
    per_event = render_terminal(
        report,
        options=TerminalOptions(width=80, count_grouping=CountGrouping.EVENT),
    )

    assert "■×15 ■×15" in per_event
    assert "×N exact per event kind" in per_event
    assert "■×30" in per_visual
    assert "■×15" not in per_visual
    assert "×N exact per visual" in per_visual
    assert "Events    30" in per_event and "Events    30" in per_visual


def test_visual_count_grouping_uses_configured_color_even_without_ansi_output() -> None:
    markers = tuple(
        _classified(
            f"modified-{index}",
            8,
            0,
            source=Source.FILESYSTEM,
            within_schedule=True,
            timestamp_kind=TimestampKind.FS_MODIFIED,
        )
        for index in range(15)
    ) + tuple(
        _classified(
            f"birth-{index}",
            8,
            0,
            source=Source.FILESYSTEM,
            within_schedule=True,
            timestamp_kind=TimestampKind.FS_CREATED,
        )
        for index in range(15)
    )
    styles = compile_event_style_sheet(
        (
            parse_event_style_rules(
                {"fs:file:birth": {"color": "magenta"}},
                location="styles",
            ),
        )
    )
    report = _report(*markers)

    plain = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            color=False,
            count_grouping=CountGrouping.VISUAL,
            event_styles=styles,
        ),
    )
    colored = render_terminal(
        report,
        options=TerminalOptions(
            width=80,
            color=True,
            count_grouping=CountGrouping.VISUAL,
            event_styles=styles,
        ),
    )

    assert "■×15 ■×15" in plain
    assert "■×30" not in plain
    assert colored.count("×15") == 2
    assert "\x1b[94m■×15\x1b[0m" in colored
    assert "\x1b[35m■×15\x1b[0m" in colored


def test_visual_count_grouping_merges_folding_precompacted_event_kinds() -> None:
    markers = tuple(
        _classified(
            f"event-{index:03d}",
            8,
            0,
            source=Source.FILESYSTEM,
            within_schedule=True,
            timestamp_kind=(TimestampKind.FS_MODIFIED if index % 2 else TimestampKind.FS_CREATED),
            fractional_nanoseconds=index * 1_000,
        )
        for index in range(300)
    )
    report = _report(*markers)
    cell = report.aggregation.clusters[0].cell(Weekday.MONDAY)

    assert cell is not None and cell.compacted

    per_visual = render_terminal(report, options=TerminalOptions(width=80))
    per_event = render_terminal(
        report,
        options=TerminalOptions(width=80, count_grouping=CountGrouping.EVENT),
    )

    assert per_event.count("■×150") == 2
    assert "■×300" in per_visual
    assert "Events    300" in per_event and "Events    300" in per_visual


@pytest.mark.parametrize("width", [60, 80, 120])
@pytest.mark.parametrize("grid_style", tuple(GridStyle))
def test_sparse_chart_respects_supported_terminal_widths(width: int, grid_style: GridStyle) -> None:
    markers = tuple(_classified(f"event-{index}", 8, 0, source=Source.GIT, within_schedule=True) for index in range(25))

    rendered = render_terminal(_report(*markers), options=TerminalOptions(width=width, grid_style=grid_style))

    assert all(display_width(line) <= width for line in rendered.splitlines())


def test_large_exact_count_wraps_without_exceeding_a_narrow_column() -> None:
    markers = tuple(
        _classified(
            f"event-{index}",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            fractional_nanoseconds=1,
        )
        for index in range(1_000)
    )

    rendered = render_terminal(_report(*markers), options=TerminalOptions(width=60))

    assert "●×" in rendered and "1000" in rendered
    assert "08:00" in rendered and "08:00:00" not in rendered
    assert "Events    1,000" in rendered
    assert all(display_width(line) <= 60 for line in rendered.splitlines())


def test_no_color_shapes_preserve_source_and_schedule_status() -> None:
    report = _report(
        _classified("git-inside", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("fs-inside", 8, 1, source=Source.FILESYSTEM, within_schedule=True),
        _classified("git-outside", 18, 0, source=Source.GIT, within_schedule=False),
        _classified("fs-outside", 18, 1, source=Source.FILESYSTEM, within_schedule=False),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))

    assert "●■" in rendered
    assert "○□" in rendered
    assert "● Git · ■ Filesystem · ○/□ Outside working hours" in rendered
    assert "×N exact per event kind" not in rendered
    assert "\x1b[" not in rendered


def test_legend_describes_only_visible_matrix_categories() -> None:
    report = _report(
        _classified("visible", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("cropped", 18, 0, source=Source.FILESYSTEM, within_schedule=False),
        display_range=(8 * 60, 9 * 60),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))

    assert "● Git" in rendered
    assert "Filesystem" not in rendered
    assert "Outside working hours" not in rendered
    assert "×N exact per event kind" not in rendered
    assert re.search(r"^Hidden\s+after display 1$", rendered, re.MULTILINE)


def test_explicitly_hidden_weekend_columns_keep_summary_accountability() -> None:
    report = _report(
        _classified("visible-monday", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("hidden-saturday", 8, 0, source=Source.FILESYSTEM, within_schedule=False, day=8),
        _classified("hidden-sunday", 9, 0, source=Source.GIT, within_schedule=False, day=9),
        hide_days=(Weekday.SATURDAY, Weekday.SUNDAY),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))
    header = rendered.splitlines()[0]

    assert all(day in header for day in ("Mon", "Tue", "Wed", "Thu", "Fri"))
    assert "Sat" not in header and "Sun" not in header
    assert "Filesystem" not in rendered
    assert "Outside working hours" not in rendered
    assert "Events    3" in rendered
    assert "Calendar  1 weekday (33.3%) · 2 weekend (66.7%)" in rendered
    assert re.search(r"^Hidden\s+2 events in Sat, Sun columns$", rendered, re.MULTILINE)
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_empty_day_hiding_keeps_only_occupied_columns_without_a_hidden_notice() -> None:
    report = _report(
        _classified("monday", 8, 0, source=Source.GIT, within_schedule=True),
        _classified("saturday", 9, 0, source=Source.GIT, within_schedule=False, day=8),
        hide_empty_days=tuple(Weekday),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))
    header = rendered.splitlines()[0]

    assert "Mon" in header and "Sat" in header
    assert all(day not in header for day in ("Tue", "Wed", "Thu", "Fri", "Sun"))
    assert not re.search(r"^Hidden\s+", rendered, re.MULTILINE)
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_explicit_day_hiding_can_leave_an_accounted_empty_matrix() -> None:
    report = _report(
        _classified("hidden-saturday", 9, 0, source=Source.GIT, within_schedule=False, day=8),
        hide_days=(Weekday.SATURDAY, Weekday.SUNDAY),
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, color=False))

    assert "No events in the displayed weekday/time range." in rendered
    assert "Events    1" in rendered
    assert re.search(r"^Hidden\s+1 event in Sat column$", rendered, re.MULTILINE)


def test_hiding_all_empty_days_has_a_compact_empty_state() -> None:
    rendered = render_terminal(
        _report(hide_empty_days=tuple(Weekday)),
        options=TerminalOptions(width=80, color=False),
    )

    assert rendered.splitlines()[0] == "Time band"
    assert "No occupied days." in rendered
    assert "Events    0" in rendered


def test_identity_legend_maps_only_identities_visible_after_cropping() -> None:
    report = _report(
        _classified(
            "visible",
            8,
            0,
            source=Source.GIT,
            within_schedule=True,
            actor_name="Ada Visible",
            actor_email="ada@example.test",
        ),
        _classified(
            "cropped",
            18,
            0,
            source=Source.GIT,
            within_schedule=False,
            actor_name="Bob Hidden",
            actor_email="bob@example.test",
        ),
        display_range=(8 * 60, 9 * 60),
        retain_git_identities=True,
    )

    rendered = render_terminal(
        report,
        options=TerminalOptions(width=80, color=False, marker_style=MarkerStyle.IDENTITY),
    )

    assert "A Ada Visible <ada@example.test>" in rendered
    assert "Bob Hidden" not in rendered
    assert "Outside working hours" not in rendered
