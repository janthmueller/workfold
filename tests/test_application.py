from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Collection, Sequence
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from workfold.application import run
from workfold.cli import parse_options
from workfold.collectors.git import GitCommandError, GitRunner
from workfold.collectors.git_changes import GitFileChangeCollector

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
    assert "timestamp observations included: 1" in rendered


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
    assert "Git timestamps explicitly filtered by identity" in rendered


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
    assert "Git author captured: 1; requested=1, identity filtered=1" in rendered
    assert "Git committer captured: 1; requested=1, included=1, markers=1" in rendered
    assert "Git tagger captured: 1; requested=1, included=1, markers=1" in rendered


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
    assert re.search(r"Git reflog captured: \d+; .*included=1", rendered)


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
            "all",
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
    assert "Git author captured: 1; requested=1, identity filtered=1" in rendered
    assert "filesystem modified captured: 1; requested=1, included=1, markers=1" in rendered


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
    assert "timestamp slots requested: 2" in rendered
    assert "timestamp observations captured: 2" in rendered
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


def test_date_selection_precedes_identity_filter_in_coverage(tmp_path: Path) -> None:
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
    assert "Git author captured: 1; requested=1, outside date=1" in rendered
    assert "identity filtered=1" not in rendered


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
    assert "Git commit inputs for file-change derivation: discovered=1, parsed=1, record errors=0" in rendered
    assert (
        "Git file-change derivation: commits requested=1, successfully parsed=1, "
        "parse failures=0, subprocess failures=0, file changes discovered=1" in rendered
    )
    assert f"target Git commit inputs [git] {repo.path.resolve()}: discovered=1, parsed=1" in rendered
    assert f"target Git file-change derivation [git] {repo.path.resolve()}:" in rendered


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
