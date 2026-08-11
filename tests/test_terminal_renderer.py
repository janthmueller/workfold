from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from workfold.aggregation import aggregate_markers
from workfold.config import GridStyle, MarkerStyle
from workfold.models import (
    ActivityMarker,
    ClassifiedMarker,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    Weekday,
)
from workfold.renderers.terminal import TerminalOptions, render_terminal, terminal_color_enabled
from workfold.reports import COMPLETE_COVERAGE_STATUS, ReportContext, build_report
from workfold.sanitization import display_width, sanitize_terminal_text, truncate_end, truncate_middle


def _classified(
    identifier: str,
    hour: int,
    minute: int,
    *,
    second: int = 0,
    source: Source,
    within_schedule: bool,
    description: str | None = None,
    root: Path = Path("/work/repository"),
    fractional_nanoseconds: int = 0,
    actor_name: str = "Fixture",
    actor_email: str = "fixture@example.test",
    day: int = 3,
) -> ClassifiedMarker:
    local_datetime = datetime(
        2026,
        8,
        day,
        hour,
        minute,
        second,
        microsecond=fractional_nanoseconds // 1_000,
        tzinfo=timezone.utc,
    )
    is_git = source is Source.GIT
    origin = RecordOrigin(
        record_id=f"record-{identifier}",
        source=source,
        record_kind=RecordKind.COMMIT if is_git else RecordKind.FILESYSTEM_ENTRY,
        repository_or_root=root,
        path=None if is_git else Path(f"files/{identifier}"),
        commit_id=identifier if is_git else None,
        description=description,
    )
    kind = TimestampKind.GIT_AUTHOR if is_git else TimestampKind.FS_MODIFIED
    instant_ns = int(local_datetime.replace(microsecond=0).timestamp()) * 1_000_000_000 + fractional_nanoseconds
    marker = ActivityMarker.create(
        (
            TimestampObservation.create(
                origin,
                kind,
                instant_ns,
                str(instant_ns),
                original_offset_minutes=0 if is_git else None,
                actor_name=actor_name if is_git else None,
                actor_email=actor_email if is_git else None,
            ),
        )
    )
    return ClassifiedMarker(
        marker,
        local_datetime,
        within_schedule,
    )


def _report(
    *markers: ClassifiedMarker,
    outside_limit: int = 50,
    cluster_window: timedelta = timedelta(hours=1),
    display_range: tuple[int, int] | None = None,
    retain_git_identities: bool = False,
    hide_days: tuple[Weekday, ...] = (),
    hide_empty_days: tuple[Weekday, ...] = (),
):
    aggregation = aggregate_markers(
        markers,
        cluster_window=cluster_window,
        display_range=display_range,
        outside_limit=outside_limit,
        retain_git_identities=retain_git_identities,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
    )
    context = ReportContext(
        source_label="Git commits + filesystem metadata",
        range_label="2026-W32",
        timezone_label="UTC",
        schedule_label="Mo-Fr 08:00-16:30",
        coverage_status=COMPLETE_COVERAGE_STATUS,
        enabled_sources=(Source.GIT, Source.FILESYSTEM),
        enabled_record_kinds=(RecordKind.COMMIT, RecordKind.FILESYSTEM_ENTRY),
        identity_label="all recorded identities",
        ignore_label="respecting Git ignore rules",
    )
    return build_report(aggregation, context)


def _coalesced_git(
    identifier: str,
    hour: int,
    *,
    within_schedule: bool,
) -> ClassifiedMarker:
    local_datetime = datetime(2026, 8, 3, hour, tzinfo=timezone.utc)
    instant_ns = int(local_datetime.timestamp()) * 1_000_000_000
    origin = RecordOrigin(
        record_id=f"record-{identifier}",
        source=Source.GIT,
        record_kind=RecordKind.COMMIT,
        repository_or_root=Path("/work/repository"),
        commit_id=identifier,
    )
    author = TimestampObservation.create(
        origin,
        TimestampKind.GIT_AUTHOR,
        instant_ns,
        str(instant_ns),
        original_offset_minutes=0,
        actor_name="Ada Author",
        actor_email="ada@example.test",
    )
    committer = TimestampObservation.create(
        origin,
        TimestampKind.GIT_COMMITTER,
        instant_ns,
        str(instant_ns),
        original_offset_minutes=0,
        actor_name="Bob Committer",
        actor_email="bob@example.test",
    )
    return ClassifiedMarker(ActivityMarker.create((author, committer)), local_datetime, within_schedule)


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
    assert "Workfold ·" not in rendered
    assert "08:05–08:09" in rendered
    assert "●□" in rendered
    assert "G=" not in rendered and "F=" not in rendered and "M=" not in rendered
    assert "Density:" not in rendered
    assert "Events    2" in rendered
    assert "Schedule  1 inside (50.0%) · 1 outside (50.0%)" in rendered
    assert "Calendar  2 weekday (100.0%) · 0 weekend (0.0%)" in rendered
    assert "● Git · ■ Filesystem · □ Outside working hours" in rendered
    assert "Working hours: Mo-Fr 08:00-16:30" in rendered
    assert "×N exact count" not in rendered
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
    assert "×N exact count" in rendered
    assert "Events    26" in rendered


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
    assert "×N exact count" not in rendered
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
    assert "×N exact count" not in rendered
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


@pytest.mark.parametrize(
    ("no_color", "environment", "is_tty", "expected"),
    [
        (False, {}, True, True),
        (True, {}, True, False),
        (False, {"NO_COLOR": ""}, True, False),
        (False, {"TERM": "dumb"}, True, False),
        (False, {}, False, False),
    ],
)
def test_terminal_color_policy(
    no_color: bool,
    environment: dict[str, str],
    is_tty: bool,
    expected: bool,
) -> None:
    assert terminal_color_enabled(no_color=no_color, environ=environment, stdout_is_tty=is_tty) is expected


def test_outside_list_is_bounded_chronological_sanitized_and_80_columns() -> None:
    hostile = "subject\n\x1b[31mforged"
    report = _report(
        _classified("old", 6, 0, source=Source.GIT, within_schedule=False),
        _classified("new", 20, 0, source=Source.GIT, within_schedule=False, description=hostile),
        outside_limit=1,
    )

    rendered = render_terminal(report, options=TerminalOptions(width=80, list_outside=True))

    assert "showing 1 of 2; 1 older omitted" in rendered
    assert "2026-08-03T20:00:00+00:00" in rendered
    assert "2026-08-03T06:00:00+00:00" not in rendered
    assert r"subject\n\x1b[31mforged" in rendered
    assert "\x1b[31mforged" not in rendered.replace(r"\x1b[31mforged", "")
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_outside_list_preserves_fractional_nanoseconds() -> None:
    report = _report(
        _classified(
            "precise",
            20,
            0,
            source=Source.FILESYSTEM,
            within_schedule=False,
            fractional_nanoseconds=123_456_789,
        )
    )

    rendered = render_terminal(report, options=TerminalOptions(width=100, list_outside=True))

    assert "2026-08-03T20:00:00.123456789+00:00" in rendered


def test_zero_event_summary_uses_na_percentages() -> None:
    rendered = render_terminal(_report(), options=TerminalOptions(width=80))

    assert "Events    0" in rendered
    assert rendered.count("n/a") == 4
    assert "Breakdown" not in rendered


def test_verbose_renderer_restores_operational_and_exact_scope_details() -> None:
    report = _report(_classified("event", 8, 0, source=Source.GIT, within_schedule=True))
    context = replace(
        report.context,
        extent_label="whole Git repositories=/work/repository",
        exclusions=("*.tmp",),
    )

    rendered = render_terminal(
        replace(report, context=context),
        options=TerminalOptions(width=80, verbose=True),
    )

    assert rendered.index("Events") < rendered.index("Details")
    assert "Scope: Git + filesystem · standard" in rendered
    assert "Period: 2026-W32 · UTC" in rendered
    assert "Schedule: Mo-Fr 08:00-16:30" in rendered
    assert f"Coverage: {COMPLETE_COVERAGE_STATUS}" in rendered
    assert "Cluster window: 1h" in rendered
    assert "Compression: empty time omitted; busy cells use exact symbol×count" in rendered
    assert "Collector selectors: Git commits + filesystem metadata" in rendered
    assert "Git identities: all recorded identities" in rendered
    assert "Extents: whole Git repositories=/work/repository" in rendered
    assert "Filesystem policy: respecting Git ignore rules" in rendered
    assert "Explicit exclusions: *.tmp" in rendered
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_requested_coverage_details_remain_visible_without_verbose_configuration() -> None:
    report = _report()
    context = replace(
        report.context,
        coverage_details=("timestamp slots requested: 4", "activity markers plotted: 2"),
    )

    rendered = render_terminal(replace(report, context=context), options=TerminalOptions(width=80))

    assert "Coverage details:" in rendered
    assert "timestamp slots requested: 4" in rendered
    assert "activity markers plotted: 2" in rendered
    assert COMPLETE_COVERAGE_STATUS not in rendered
    assert "Details\n" not in rendered


def test_long_scope_facts_wrap_between_words() -> None:
    report = _report()
    context = replace(
        report.context,
        coverage_status=(
            f"{COMPLETE_COVERAGE_STATUS}; "
            "explicit exclusions active for generated artifacts in deeply nested output directories"
        ),
    )

    rendered = render_terminal(replace(report, context=context), options=TerminalOptions(width=80))

    assert "Coverage" in rendered
    assert COMPLETE_COVERAGE_STATUS not in rendered
    assert "expli\ncit" not in rendered
    assert "explicit exclusions active for generated artifacts" in rendered


def test_partial_coverage_status_remains_intact_in_default_summary() -> None:
    report = _report()
    context = replace(report.context, coverage_status="partial · 1 collection error; explicit exclusions active")

    rendered = render_terminal(replace(report, context=context), options=TerminalOptions(width=80))

    assert "Coverage  partial · 1 collection error; explicit exclusions active" in rendered


def test_sanitization_and_width_helpers_keep_untrusted_text_single_line() -> None:
    assert sanitize_terminal_text("a\tb\n\x1bc\u202ed\u2028e\u2029f") == (r"a\tb\n\x1bc\u202ed\u2028e\u2029f")
    assert display_width("A界") == 3
    assert truncate_end("abcdef", 4) == "abc…"
    assert truncate_middle("abcdefgh", 5) == "ab…gh"


def test_terminal_options_reject_too_narrow_layout() -> None:
    with pytest.raises(ValueError, match="at least 60"):
        TerminalOptions(width=59)
