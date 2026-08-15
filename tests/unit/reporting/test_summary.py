from dataclasses import replace

import pytest
from workfold.application.report import DiagnosticFacts
from workfold.configuration import BandLabel, ClusterAnchor, EventListSelection, GridStyle, MarkerStyle
from workfold.domain.evidence import EvidenceKind
from workfold.domain.observations import Source
from workfold.reporting.sanitization import display_width, sanitize_terminal_text, truncate_end, truncate_middle
from workfold.reporting.terminal import TerminalOptions, render_terminal, terminal_color_enabled
from workfold.reporting.terminal.coverage_status import COMPLETE_COVERAGE_STATUS

from support.reports import classified_marker, coalesced_git_marker, report


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
    rendered = render_terminal(
        report(
            classified_marker("old", 6, 0, source=Source.GIT, within_schedule=False),
            classified_marker("new", 20, 0, source=Source.GIT, within_schedule=False, description=hostile),
            event_limit=1,
        ),
        options=TerminalOptions(width=80, show_event_list=True),
    )

    assert "showing 1 of 2; 1 additional omitted" in rendered
    assert "Events outside working hours" in rendered
    assert "Timestamp                              Schedule" not in rendered
    assert "2026-08-03T20:00:00+00:00" in rendered
    assert "2026-08-03T06:00:00+00:00" not in rendered
    assert r"subject\n\x1b[31mforged" in rendered
    assert "\x1b[31mforged" not in rendered.replace(r"\x1b[31mforged", "")
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_outside_list_preserves_fractional_nanoseconds() -> None:
    rendered = render_terminal(
        report(
            classified_marker(
                "precise",
                20,
                0,
                source=Source.FILESYSTEM,
                within_schedule=False,
                fractional_nanoseconds=123_456_789,
            )
        ),
        options=TerminalOptions(width=100, show_event_list=True),
    )
    assert "2026-08-03T20:00:00.123456789+00:00" in rendered
    assert "  fs:file:modified\n" in rendered
    assert "  /work/repository\n" in rendered
    assert "  files/precise\n" in rendered


def test_all_event_list_shows_each_rows_schedule_state() -> None:
    rendered = render_terminal(
        report(
            classified_marker("inside", 10, 0, source=Source.GIT, within_schedule=True),
            classified_marker("outside", 20, 0, source=Source.GIT, within_schedule=False),
            event_list=EventListSelection(),
        ),
        options=TerminalOptions(width=80, show_event_list=True),
    )

    assert "Timestamp                              Schedule" in rendered
    timestamp_lines = tuple(line for line in rendered.splitlines() if line.startswith("2026-"))
    assert any(line.endswith("inside") for line in timestamp_lines)
    assert any(line.endswith("outside") for line in timestamp_lines)


def test_exact_event_list_projects_only_selected_role_from_a_coalesced_marker() -> None:
    rendered = render_terminal(
        report(
            coalesced_git_marker("abc123", 10, within_schedule=True),
            event_list=EventListSelection(evidence_kinds=(EvidenceKind.GIT_COMMIT_COMMITTER,)),
        ),
        options=TerminalOptions(width=80, show_event_list=True),
    )

    assert "git:commit:committer" in rendered
    assert "git:commit:{author,committer}" not in rendered


def test_zero_event_summary_uses_na_percentages() -> None:
    rendered = render_terminal(report(), options=TerminalOptions(width=80))
    assert "Events    0" in rendered
    assert rendered.count("n/a") == 4
    assert "Breakdown" not in rendered


def test_verbose_renderer_restores_operational_and_exact_scope_details() -> None:
    value = report(classified_marker("event", 8, 0, source=Source.GIT, within_schedule=True))
    context = replace(value.context, scope=replace(value.context.scope, exclusions=("*.tmp",)))
    rendered = render_terminal(replace(value, context=context), options=TerminalOptions(width=80, verbose=True))

    assert rendered.index("Events") < rendered.index("Details")
    assert "Scope: Git + filesystem · standard" in rendered
    assert "Period: 2026-W32 · UTC" in rendered
    assert "Schedule: Mo-Fr 08:00-16:30" in rendered
    assert f"Coverage: {COMPLETE_COVERAGE_STATUS}" in rendered
    assert "Cluster anchor: event" in rendered
    assert "Cluster window: 1h" in rendered
    assert "Band labels: range" in rendered
    assert "Compression: empty time omitted; gaps of at least one cluster window" in rendered
    assert "busy cells use exact symbol×count" in rendered
    assert "Collector selectors: git:commit:author, fs:file:birth, fs:file:modified; commits" in rendered
    assert "from local branches + detached HEAD" in rendered
    assert "Git identities: all recorded identities" in rendered
    assert "Extents:" not in rendered
    assert "Filesystem policy: standard Git ignores respected; files" in rendered
    assert "Explicit exclusions: *.tmp" in rendered
    assert all(display_width(line) <= 80 for line in rendered.splitlines())


def test_verbose_renderer_describes_dense_fixed_bands() -> None:
    value = report(
        classified_marker("event", 8, 0, source=Source.GIT, within_schedule=True),
        cluster_anchor=ClusterAnchor.MIDNIGHT,
        display_range=(8 * 60, 10 * 60),
    )
    rendered = render_terminal(
        value,
        options=TerminalOptions(width=80, verbose=True, show_empty_bands=True),
    )
    assert "Compression: disabled; all fixed bands in the display range shown" in rendered
    assert "empty time omitted" not in rendered


def test_requested_coverage_details_remain_visible_without_verbose_configuration() -> None:
    rendered = render_terminal(report(), options=TerminalOptions(width=80, coverage=True))
    assert "Coverage details:" in rendered
    assert "timestamp slots examined: 0" in rendered
    assert "activity markers plotted: 0" in rendered
    assert COMPLETE_COVERAGE_STATUS not in rendered
    assert "Details\n" not in rendered


def test_long_scope_facts_wrap_between_words() -> None:
    value = report()
    exclusion = "generated artifacts in deeply nested output directories"
    context = replace(value.context, scope=replace(value.context.scope, exclusions=(exclusion,)))
    rendered = render_terminal(replace(value, context=context), options=TerminalOptions(width=80, verbose=True))
    assert "Coverage" in rendered
    assert "gene\nrated" not in rendered
    assert "generated artifacts in deeply nested output" in rendered


def test_partial_coverage_status_remains_intact_in_default_summary() -> None:
    value = report()
    context = replace(
        value.context,
        collection=replace(value.context.collection, diagnostics=DiagnosticFacts(errors=1)),
        scope=replace(value.context.scope, exclusions=("*.tmp",)),
    )
    rendered = render_terminal(replace(value, context=context), options=TerminalOptions(width=80))
    assert "Coverage  partial · 1 collection error; explicit exclusions active" in rendered


def test_sanitization_and_width_helpers_keep_untrusted_text_single_line() -> None:
    assert sanitize_terminal_text("a\tb\n\x1bc\u202ed\u2028e\u2029f") == (r"a\tb\n\x1bc\u202ed\u2028e\u2029f")
    assert display_width("A界") == 3
    assert truncate_end("abcdef", 4) == "abc…"
    assert truncate_middle("abcdefgh", 5) == "ab…gh"


def test_terminal_options_reject_too_narrow_layout() -> None:
    with pytest.raises(ValueError, match="at least 60"):
        TerminalOptions(width=59)


def test_terminal_options_preserve_existing_positional_enum_arguments() -> None:
    options = TerminalOptions(80, False, False, False, MarkerStyle.IDENTITY, GridStyle.BOTH)
    assert options.marker_style is MarkerStyle.IDENTITY
    assert options.grid_style is GridStyle.BOTH
    assert options.band_label is BandLabel.RANGE
    assert not options.show_empty_bands


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("marker_style", "identity", "marker_style"),
        ("grid_style", "both", "grid_style"),
        ("band_label", "start", "band_label"),
        ("show_empty_bands", "yes", "show_empty_bands"),
        ("coverage", "yes", "coverage"),
    ],
)
def test_terminal_options_reject_unresolved_enum_values(keyword: str, value: str, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        TerminalOptions(**{keyword: value})  # type: ignore[arg-type]
