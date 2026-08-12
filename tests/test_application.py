from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Collection, Sequence
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workfold.app.collection import collect as collect_sources
from workfold.app.coverage import build_coverage
from workfold.application import run
from workfold.cli import parse_options
from workfold.collectors.git import GitCollector, GitCommandError, GitRunner
from workfold.collectors.git_changes import GitFileChangeCollector
from workfold.coverage import CoverageInvariantError
from workfold.models import TimestampKind, TimestampObservation
from workfold.pipeline import ObservationBatch
from workfold.scope import ObservationScope
from workfold.time_ranges import InstantRange, InstantRangeUnion, datetime_to_utc_ns

from support.git_repo import GitRepo

BERLIN = ZoneInfo("Europe/Berlin")


def _git_date(value: datetime) -> str:
    offset = value.strftime("%z")
    return f"@{int(value.timestamp())} {offset}"


def _assert_summary_count(rendered: str, label: str, count: int) -> None:
    assert re.search(rf"^{re.escape(label)}\s+{count:,}$", rendered, re.MULTILINE)


def _assert_summary_right_count(rendered: str, label: str, count: int, percentage: str) -> None:
    assert re.search(
        rf"^Schedule\s+.*{count:,} {re.escape(label.casefold())} \({re.escape(percentage)}\)$",
        rendered,
        re.MULTILINE,
    )


@pytest.mark.parametrize("record_kind", ("file-change", "tag", "reflog"))
def test_collector_scope_accounting_detects_a_dropped_pipeline_batch(
    tmp_path: Path,
    record_kind: str,
) -> None:
    repo = GitRepo.create(tmp_path / record_kind)
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    commit_id = repo.commit(
        "work.txt",
        "one",
        "scope evidence",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    repo.run(
        "tag",
        "-a",
        "scope-evidence",
        commit_id,
        "-m",
        "scope evidence",
        environment={
            "GIT_COMMITTER_DATE": _git_date(instant),
            "GIT_COMMITTER_NAME": "Fixture Tagger",
            "GIT_COMMITTER_EMAIL": "tagger@example.test",
        },
    )
    options = parse_options(
        [
            str(repo.path),
            "--git-records",
            record_kind,
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
        ]
    )
    scope = ObservationScope(InstantRangeUnion((InstantRange(None, None),)))
    collection = collect_sources(
        options,
        observation_consumer=lambda _batch: None,
        observation_scope=scope,
        git_collector=None,
        repository_resolver=None,
        file_change_collector=None,
        tag_collector=None,
        reflog_collector=None,
        filesystem_collector=None,
    )

    with pytest.raises(CoverageInvariantError, match="scope matches"):
        build_coverage(collection, options, observations={}, plotting={})


def test_git_commit_flows_through_selection_schedule_and_terminal_report(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    outside = datetime(2026, 8, 3, 7, 59, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "before the working day",
        author_date=_git_date(outside),
        committer_date=_git_date(outside),
        author_name="Ada Person",
        author_email="ada@example.test",
    )
    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
            "--cluster-window",
            "1h5m",
            "--no-color",
            "--coverage",
            "--verbose",
            "--list-outside",
        ]
    )

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    rendered = output.getvalue()
    assert status == 0
    assert not errors.getvalue()
    assert "Cluster window: 1h5m" in rendered
    assert "Mo-Fr 08:00-16:30" in rendered
    assert "inside" in rendered.casefold()
    assert "outside" in rendered.casefold()
    assert "07:59     ○" in rendered
    _assert_summary_count(rendered, "Events", 1)
    _assert_summary_right_count(rendered, "Outside", 1, "100.0%")
    assert "2026-08-03T07:59:00+02:00" in rendered
    assert "before the working day" in rendered
    assert "Git commits discovered: 1" in rendered
    assert "timestamp observations selected: 1" in rendered


def test_rolling_time_selector_uses_one_half_open_elapsed_window(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    now = datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("UTC"))
    start = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("UTC"))
    repo.commit(
        "included.txt",
        "one",
        "exactly at rolling start",
        author_date=_git_date(start),
        committer_date=_git_date(start),
    )
    repo.commit(
        "excluded.txt",
        "two",
        "exactly at captured now",
        author_date=_git_date(now),
        committer_date=_git_date(now),
    )
    output = StringIO()
    options = parse_options([str(repo.path), "--time", "2d", "--timezone", "UTC", "--no-color", "--verbose"])

    assert run(options, now=now, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Period: last 2d · UTC" in rendered


def test_identity_marker_style_uses_recorded_git_identity_letters(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "ada.txt",
        "one",
        "Ada event",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        author_name="Ada Person",
        author_email="ada@example.test",
    )
    repo.commit(
        "alice.txt",
        "two",
        "Alice event",
        author_date=_git_date(instant.replace(minute=1)),
        committer_date=_git_date(instant.replace(minute=1)),
        author_name="Alice Person",
        author_email="alice@example.test",
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--marker-style",
            "identity",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    assert "A1A2" in rendered
    assert "A1 Ada Person <ada@example.test>" in rendered
    assert "A2 Alice Person <alice@example.test>" in rendered
    assert "●" not in rendered and "○" not in rendered


def test_day_column_controls_flow_from_cli_without_changing_event_totals(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    for filename, instant in (
        ("monday.txt", datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)),
        ("saturday.txt", datetime(2026, 8, 8, 10, 0, tzinfo=BERLIN)),
    ):
        repo.commit(
            filename,
            filename,
            filename,
            author_date=_git_date(instant),
            committer_date=_git_date(instant),
        )

    hidden_output = StringIO()
    hidden = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--hide-days",
            "weekend",
            "--no-color",
        ]
    )
    assert run(hidden, stdout=hidden_output, stderr=StringIO(), terminal_width=80) == 0

    hidden_rendered = hidden_output.getvalue()
    hidden_header = hidden_rendered.splitlines()[0]
    assert "Sat" not in hidden_header and "Sun" not in hidden_header
    assert re.search(r"^Events\s+2$", hidden_rendered, re.MULTILINE)
    assert re.search(r"^Hidden\s+1 event in Sat column$", hidden_rendered, re.MULTILINE)

    conditional_output = StringIO()
    conditional = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--hide-empty-days",
            "weekend",
            "--no-color",
        ]
    )
    assert run(conditional, stdout=conditional_output, stderr=StringIO(), terminal_width=80) == 0

    conditional_rendered = conditional_output.getvalue()
    conditional_header = conditional_rendered.splitlines()[0]
    assert "Sat" in conditional_header and "Sun" not in conditional_header
    assert not re.search(r"^Hidden\s+", conditional_rendered, re.MULTILINE)


def test_grid_style_flows_from_cli_to_terminal_rendering(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "grid",
        "grid",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--grid",
            "both",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    chart_lines = output.getvalue().split("\n\n", maxsplit=1)[0].splitlines()
    assert "│" in chart_lines[0]
    assert "┼" in chart_lines[1]


def test_git_identity_filter_is_case_insensitive_literal_or_and_accounted(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "selected by email",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        author_name="Ada Person",
        author_email="ADA@EXAMPLE.TEST",
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--git-identity",
            "nobody",
            "--git-identity",
            "ada@example",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Git identities: nobody OR ada@example" in rendered
    assert "Git identity scope active" in rendered


def test_git_identity_filter_uses_each_timestamp_roles_own_identity(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    author_instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    committer_instant = datetime(2026, 8, 4, 11, 0, tzinfo=BERLIN)
    tagger_instant = datetime(2026, 8, 5, 12, 0, tzinfo=BERLIN)
    commit_id = repo.commit(
        "work.txt",
        "one",
        "different identities",
        author_date=_git_date(author_instant),
        committer_date=_git_date(committer_instant),
        author_name="Original Author",
        author_email="author@example.test",
        committer_name="Later Committer",
        committer_email="COMMITTER@EXAMPLE.TEST",
    )
    repo.run(
        "tag",
        "-a",
        "v1.0.0",
        commit_id,
        "-m",
        "tagged release",
        environment={
            "GIT_COMMITTER_DATE": _git_date(tagger_instant),
            "GIT_COMMITTER_NAME": "Release Tagger",
            "GIT_COMMITTER_EMAIL": "tagger@example.test",
        },
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--profile",
            "portable",
            "--git-identity",
            "committer@example",
            "--git-identity",
            "RELEASE TAGGER",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 2)
    assert "Git identities: committer@example OR RELEASE TAGGER" in rendered
    assert "Git author selected: 0; examined=1, values read=1" in rendered
    assert "Git committer selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered
    assert "Git tagger selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered


def test_git_identity_filter_matches_reflog_actor(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    commit_id = repo.commit(
        "work.txt",
        "one",
        "reflog fixture",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    repo.run(
        "update-ref",
        "--create-reflog",
        "-m",
        "manual activity",
        "refs/custom/activity",
        commit_id,
        environment={
            "GIT_COMMITTER_DATE": _git_date(instant),
            "GIT_COMMITTER_NAME": "Reflog Operator",
            "GIT_COMMITTER_EMAIL": "operator@example.test",
        },
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--git-records",
            "reflog",
            "--git-identity",
            "operator@example",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Git identities: operator@example" in rendered
    assert re.search(r"Git reflog selected: 1; .*markers=1", rendered)


def test_git_identity_filter_does_not_filter_filesystem_observations(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "combined fixture",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    timestamp = instant.timestamp()
    os.utime(repo.path / "work.txt", (timestamp, timestamp))
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--mode",
            "both",
            "--fs-times",
            "modified",
            "--git-identity",
            "does-not-match",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Git identities: does-not-match; filesystem unaffected" in rendered
    assert "Git author selected: 0; examined=1, values read=1" in rendered
    assert "filesystem modified selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered


def test_same_commit_identical_author_and_committer_dates_coalesce(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 4, 11, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "same instant",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--git-commit-times",
            "author,committer",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "timestamp slots examined: 2" in rendered
    assert "timestamp values read: 2" in rendered
    assert "coalesced for plotting with roles preserved: 1" in rendered


def test_multiple_targets_continue_but_strict_mode_returns_nonzero(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 4, 11, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "usable",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    missing = tmp_path / "missing"
    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            str(missing),
            "--time",
            "all",
            "--timezone",
            "UTC",
            "--strict",
            "--no-color",
        ]
    )

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    assert status == 1
    _assert_summary_count(output.getvalue(), "Events", 1)
    assert re.search(
        r"^Coverage\s+partial · 1 collection error",
        output.getvalue(),
        re.MULTILINE,
    )
    assert "selected path does not exist" in errors.getvalue()
    assert "Pass an existing file or directory" in errors.getvalue()


def test_no_usable_git_target_fails_with_actionable_hint(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    output = StringIO()
    errors = StringIO()
    options = parse_options([str(plain), "--timezone", "UTC", "--no-color"])

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    assert status == 2
    assert not output.getvalue()
    assert not errors.getvalue().startswith("\n")
    assert "not a Git repository" in errors.getvalue()
    assert "Use --mode fs" in errors.getvalue()


def test_empty_repository_plus_failed_target_renders_an_honest_partial_run(
    tmp_path: Path,
) -> None:
    empty_repo = GitRepo.create(tmp_path / "empty")
    missing = tmp_path / "missing"
    output = StringIO()
    errors = StringIO()
    options = parse_options([str(empty_repo.path), str(missing), "--timezone", "UTC", "--no-color"])

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    assert status == 0
    _assert_summary_count(output.getvalue(), "Events", 0)
    assert re.search(
        r"^Coverage\s+partial · 1 collection error",
        output.getvalue(),
        re.MULTILINE,
    )
    assert "selected path does not exist" in errors.getvalue()


def test_known_out_of_scope_git_timestamp_is_not_a_coverage_outcome(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "outside selected week",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        author_name="Ada Person",
        author_email="ada@example.test",
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W33",
            "--timezone",
            "Europe/Berlin",
            "--git-identity",
            "nobody",
            "--coverage",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert "Git author selected: 0; examined=1, values read=1" in rendered
    assert "outside date" not in rendered


def test_bounded_portable_profile_selects_author_and_committer_dates_independently(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    author_inside = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    committer_outside = datetime(2026, 8, 12, 11, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "author only in selected week",
        author_date=_git_date(author_inside),
        committer_date=_git_date(committer_outside),
        author_name="Original Author",
        author_email="author@example.test",
        committer_name="Later Committer",
        committer_email="committer@example.test",
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W32",
            "--profile",
            "portable",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Git author selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered
    assert "Git committer selected: 0; examined=1, values read=1" in rendered


def test_bounded_git_identity_filter_matches_only_in_range_identity(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "selected author identity",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        author_name="Ada Person",
        author_email="ADA@EXAMPLE.TEST",
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
            "--git-identity",
            "ada@example",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "Git author selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered


def test_selected_git_hydration_failure_preserves_read_and_scope_counts(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "selected but unavailable",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )

    class MissingObjectRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "cat-file":
                assert input_data is not None
                object_id = input_data.decode("ascii").strip()
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=f"{object_id} missing\n".encode(),
                    stderr=b"",
                )
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert (
        run(
            options,
            stdout=output,
            stderr=errors,
            terminal_width=120,
            git_collector=GitCollector(MissingObjectRunner(stream_output=False)),
        )
        == 0
    )

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 0)
    assert "Coverage  partial · 1 collection error" in rendered
    assert "Git author selected: 0; examined=1, values read=1, scope matches=1, materialization errors=1" in rendered
    assert "Git object is unavailable" in errors.getvalue()


def test_git_coverage_keeps_independent_repository_targets(tmp_path: Path) -> None:
    first = GitRepo.create(tmp_path / "first")
    second = GitRepo.create(tmp_path / "second")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    for repo, subject in ((first, "first event"), (second, "second event")):
        repo.commit(
            "work.txt",
            subject,
            subject,
            author_date=_git_date(instant),
            committer_date=_git_date(instant),
        )
    output = StringIO()
    options = parse_options(
        [
            str(first.path),
            str(second.path),
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=240) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 2)
    assert f"target records [git] {first.path.resolve()} commit" in rendered
    assert f"target records [git] {second.path.resolve()} commit" in rendered


def test_file_change_scope_reports_commit_inputs_and_derivation_per_repository(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "file event",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--git-records",
            "file-change",
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=240) == 0

    rendered = output.getvalue()
    assert "Git file changes discovered: 1" in rendered
    assert (
        "Git commit inputs for file-change derivation: reachable=1, examined=1, "
        "selected=1, hydrated=1, record errors=0" in rendered
    )
    assert (
        "Git file-change derivation: commits requested=1, successfully parsed=1, "
        "parse failures=0, subprocess failures=0, file changes discovered=1" in rendered
    )
    assert (
        f"target Git commit inputs [git] {repo.path.resolve()}: reachable=1, "
        "examined=1, selected=1, hydrated=1" in rendered
    )
    assert f"target Git file-change derivation [git] {repo.path.resolve()}:" in rendered


def test_bounded_file_changes_match_all_time_reference_and_diff_only_selected_commits(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    outside = datetime(2026, 7, 27, 10, 0, tzinfo=BERLIN)
    inside_author = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    inside_committer = datetime(2026, 8, 4, 10, 0, tzinfo=BERLIN)
    outside_id = repo.commit(
        "work.txt",
        "outside",
        "outside period",
        author_date=_git_date(outside),
        committer_date=_git_date(outside),
    )
    selected_id = repo.commit(
        "work.txt",
        "inside",
        "author inside only",
        author_date=_git_date(inside_author),
        committer_date=_git_date(inside_committer),
        author_name="Selected Author",
        author_email="selected@example.test",
        committer_name="Other Committer",
        committer_email="other@example.test",
    )
    options = parse_options(
        [
            str(repo.path),
            "--git-records",
            "file-change",
            "--git-commit-times",
            "author,committer",
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
            "--git-identity",
            "selected@example",
        ]
    )
    selected_range = InstantRangeUnion(
        (
            InstantRange(
                datetime_to_utc_ns(datetime(2026, 8, 3, tzinfo=BERLIN)),
                datetime_to_utc_ns(datetime(2026, 8, 10, tzinfo=BERLIN)),
            ),
        )
    )
    scope = ObservationScope(selected_range, ("selected@example",))

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__()
            self.diff_inputs: list[bytes] = []

        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "diff-tree":
                assert input_data is not None
                self.diff_inputs.append(input_data)
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    def collect_observations(
        selected_scope: ObservationScope,
        file_changes: GitFileChangeCollector,
    ) -> tuple[TimestampObservation, ...]:
        observations: list[TimestampObservation] = []

        def consume(batch: ObservationBatch) -> None:
            observations.extend(batch.observations)

        collect_sources(
            options,
            observation_consumer=consume,
            observation_scope=selected_scope,
            git_collector=None,
            repository_resolver=None,
            file_change_collector=file_changes,
            tag_collector=None,
            reflog_collector=None,
            filesystem_collector=None,
        )
        return tuple(observations)

    all_observations = collect_observations(
        ObservationScope(InstantRangeUnion((InstantRange(None, None),))),
        GitFileChangeCollector(),
    )
    runner = RecordingRunner()
    bounded_observations = collect_observations(scope, GitFileChangeCollector(runner))
    expected = tuple(observation for observation in all_observations if scope.includes(observation))

    assert [item.observation_id for item in bounded_observations] == [item.observation_id for item in expected]
    assert len(bounded_observations) == 1
    assert bounded_observations[0].kind is TimestampKind.GIT_AUTHOR
    diff_subjects = [line.split(maxsplit=1)[0] for line in b"".join(runner.diff_inputs).splitlines()]
    assert diff_subjects == [selected_id.encode()]
    assert outside_id.encode() not in diff_subjects


def test_bounded_tags_and_reflogs_exactly_match_all_time_reference(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    outside = datetime(2026, 7, 27, 10, 0, tzinfo=BERLIN)
    inside = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    outside_id = repo.commit(
        "outside.txt",
        "outside",
        "outside",
        author_date=_git_date(outside),
        committer_date=_git_date(outside),
    )
    inside_id = repo.commit(
        "inside.txt",
        "inside",
        "inside",
        author_date=_git_date(inside),
        committer_date=_git_date(inside),
    )
    for name, target, instant, actor in (
        ("outside", outside_id, outside, "outside@example.test"),
        ("inside", inside_id, inside, "selected@example.test"),
    ):
        repo.run(
            "tag",
            "-a",
            name,
            target,
            "-m",
            name,
            environment={
                "GIT_COMMITTER_DATE": _git_date(instant),
                "GIT_COMMITTER_NAME": "Fixture Tagger",
                "GIT_COMMITTER_EMAIL": actor,
            },
        )
    repo.run(
        "update-ref",
        "--create-reflog",
        "-m",
        "outside",
        "refs/custom/activity",
        outside_id,
        environment={
            "GIT_COMMITTER_DATE": _git_date(outside),
            "GIT_COMMITTER_NAME": "Fixture Operator",
            "GIT_COMMITTER_EMAIL": "outside@example.test",
        },
    )
    repo.run(
        "update-ref",
        "-m",
        "inside",
        "refs/custom/activity",
        inside_id,
        environment={
            "GIT_COMMITTER_DATE": _git_date(inside),
            "GIT_COMMITTER_NAME": "Fixture Operator",
            "GIT_COMMITTER_EMAIL": "selected@example.test",
        },
    )
    options = parse_options(
        [
            str(repo.path),
            "--git-records",
            "tag,reflog",
            "--git-identity",
            "selected@example",
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
        ]
    )
    selected_range = InstantRangeUnion(
        (
            InstantRange(
                datetime_to_utc_ns(datetime(2026, 8, 3, tzinfo=BERLIN)),
                datetime_to_utc_ns(datetime(2026, 8, 10, tzinfo=BERLIN)),
            ),
        )
    )
    scope = ObservationScope(selected_range, ("selected@example",))

    def collect_observations(selected_scope: ObservationScope) -> tuple[TimestampObservation, ...]:
        observations: list[TimestampObservation] = []

        def consume(batch: ObservationBatch) -> None:
            observations.extend(batch.observations)

        collect_sources(
            options,
            observation_consumer=consume,
            observation_scope=selected_scope,
            git_collector=None,
            repository_resolver=None,
            file_change_collector=None,
            tag_collector=None,
            reflog_collector=None,
            filesystem_collector=None,
        )
        return tuple(observations)

    all_observations = collect_observations(ObservationScope(InstantRangeUnion((InstantRange(None, None),))))
    bounded_observations = collect_observations(scope)
    expected = tuple(observation for observation in all_observations if scope.includes(observation))

    assert [item.observation_id for item in bounded_observations] == [item.observation_id for item in expected]
    assert {item.kind for item in bounded_observations} == {
        TimestampKind.GIT_TAGGER,
        TimestampKind.GIT_REFLOG,
    }
    assert all(item.actor_email == "selected@example.test" for item in bounded_observations)


def test_file_change_failure_accounts_for_commits_without_inventing_change_records(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "unreadable diff",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )

    class FailureRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            raise GitCommandError(
                code="git_command_failed",
                message="diff failed",
                command=tuple(arguments),
                cwd=cwd,
            )

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--git-records",
            "file-change",
            "--time",
            "all",
            "--timezone",
            "Europe/Berlin",
            "--coverage",
            "--no-color",
        ]
    )

    assert (
        run(
            options,
            stdout=output,
            stderr=errors,
            terminal_width=240,
            file_change_collector=GitFileChangeCollector(FailureRunner()),
        )
        == 0
    )

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 0)
    assert "Git file changes discovered: 0" in rendered
    assert re.search(
        r"^Coverage\s+partial · 1 collection error",
        rendered,
        re.MULTILINE,
    )
    assert (
        "Git file-change derivation: commits requested=1, successfully parsed=0, "
        "parse failures=0, subprocess failures=1, file changes discovered=0" in rendered
    )
    assert "diff failed" in errors.getvalue()
