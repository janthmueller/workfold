"""End-to-end tests for collector composition and collection profiles."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from workfold.application import run
from workfold.cli import parse_options

from support.git_repo import GitRepo

UTC = timezone.utc


def _git_date(value: datetime) -> str:
    return f"@{int(value.timestamp())} +0000"


def _set_times(path: Path, value: datetime) -> None:
    instant_ns = int(value.timestamp()) * 1_000_000_000
    try:
        os.utime(path, ns=(instant_ns, instant_ns), follow_symlinks=False)
    except NotImplementedError:
        # These fixtures are regular files; Windows does not expose the
        # no-follow variant even though setting their timestamps is supported.
        os.utime(path, ns=(instant_ns, instant_ns))


def _assert_summary_count(rendered: str, label: str, count: int) -> None:
    assert re.search(rf"^{re.escape(label)}\s+{count:,}$", rendered, re.MULTILINE)


def _assert_lean_success_output(rendered: str) -> None:
    assert not re.search(r"^(?:Scope|Period|Breakdown)\b", rendered, re.MULTILINE)
    assert not re.search(r"^Coverage\s{2,}", rendered, re.MULTILINE)
    assert "complete for all discoverable timestamps" not in rendered


def test_filesystem_mode_flows_through_the_shared_pipeline(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    file_path = root / "work.txt"
    file_path.write_text("work", encoding="utf-8")
    _set_times(file_path, datetime(2026, 8, 3, 10, 5, tzinfo=UTC))

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(root),
            "--mode",
            "fs",
            "--fs-times",
            "modified",
            "--include-ignored",
            "--time",
            "all",
            "--timezone",
            "UTC",
            "--coverage",
            "--no-color",
        ]
    )

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    rendered = output.getvalue()
    assert status == 0
    assert not errors.getvalue()
    assert "10:05     ■" in rendered
    _assert_summary_count(rendered, "Events", 1)
    assert "Mo-Fr 08:00-16:30" in rendered
    assert "inside" in rendered.casefold()
    assert "outside" in rendered.casefold()
    _assert_lean_success_output(rendered)
    assert "Filesystem events:" not in rendered
    assert "filesystem modified captured: 1" in rendered
    assert "Coverage details:" in rendered
    assert "Details\n" not in rendered


def test_filesystem_entry_selection_is_honored_by_the_application(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "work.txt").write_text("work", encoding="utf-8")
    output = StringIO()
    options = parse_options(
        [
            str(root),
            "--mode",
            "fs",
            "--time",
            "all",
            "--fs-times",
            "modified",
            "--fs-entries",
            "directory",
            "--include-ignored",
            "--coverage",
            "--verbose",
            "--timezone",
            "UTC",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 2)
    assert "entry type excluded=1" in rendered
    assert "Filesystem policy: ignored entries included; directories" in rendered


def test_all_mode_preserves_git_and_filesystem_as_distinct_evidence(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    repo.commit(
        "work.txt",
        "work",
        "same cell",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    _set_times(repo.path / "work.txt", instant)

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--mode",
            "all",
            "--fs-times",
            "modified",
            "--include-ignored",
            "--time",
            "all",
            "--timezone",
            "UTC",
            "--coverage",
            "--no-color",
        ]
    )

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    rendered = output.getvalue()
    assert status == 0
    assert not errors.getvalue()
    assert "10:05     ●■" in rendered
    _assert_summary_count(rendered, "Events", 2)
    _assert_lean_success_output(rendered)
    assert "Git events:" not in rendered
    assert "Filesystem events:" not in rendered


def test_full_profile_keeps_default_report_lean_and_coverage_opt_in(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    repo.commit(
        "work.txt",
        "work",
        "full fixture",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    output = StringIO()
    options = parse_options([str(repo.path), "--time", "all", "--profile", "full", "--timezone", "UTC", "--no-color"])

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert re.search(r"^Events\s+\d+$", rendered, re.MULTILINE)
    _assert_lean_success_output(rendered)
    assert "Mo-Fr 08:00-16:30" in rendered
    assert "Coverage details:" not in rendered
    assert "Details\n" not in rendered


def test_full_all_mode_collects_all_record_families_and_reports_capabilities(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    repo.commit(
        "work.txt",
        "work",
        "exhaustive fixture",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    repo.run(
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "fixture release",
        environment={
            "GIT_COMMITTER_DATE": _git_date(instant),
            "GIT_COMMITTER_NAME": "Fixture Tagger",
            "GIT_COMMITTER_EMAIL": "tagger@example.test",
        },
    )
    _set_times(repo.path / "work.txt", instant)
    excluded = repo.path / "skip.tmp"
    excluded.write_text("excluded", encoding="utf-8")
    _set_times(excluded, instant)

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--mode",
            "all",
            "--profile",
            "full",
            "--coverage",
            "--author",
            "Fixture",
            "--exclude",
            "*.tmp",
            "--timezone",
            "UTC",
            "--display-hours",
            "08:00-18:00",
            "--no-color",
        ]
    )

    status = run(options, stdout=output, stderr=errors, terminal_width=80)

    rendered = output.getvalue()
    assert status == 0
    assert not errors.getvalue()
    assert "Git commits discovered: 1" in rendered
    assert "Git file changes discovered: 1" in rendered
    assert "Git tags discovered: 1" in rendered
    assert "Git reflog entries discovered:" in rendered
    assert "filesystem entries discovered:" in rendered
    assert "Git tagger captured: 1" in rendered
    assert "filesystem created captured:" in rendered
    assert "explicitly excluded=1" in rendered
    assert "Coverage details:" in rendered
    assert "Details\n" not in rendered
    assert not re.search(r"^(?:Scope|Period|Breakdown)\b", rendered, re.MULTILINE)
    if "filesystem creation/birth time:" in rendered:
        normalized = " ".join(rendered.split())
        if "filesystem creation/birth time: unsupported" in normalized:
            assert "filesystem creation/birth time unavailable:" in normalized
            assert "unsupported capability result" not in rendered


def test_portable_collects_only_git_object_timestamp_evidence(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    repo.commit(
        "work.txt",
        "work",
        "portable fixture",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    repo.run(
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "portable tag",
        environment={
            "GIT_COMMITTER_DATE": _git_date(instant),
            "GIT_COMMITTER_NAME": "Fixture Tagger",
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
            "--coverage",
            "--timezone",
            "UTC",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=120) == 0

    rendered = output.getvalue()
    assert "Git commits discovered: 1" in rendered
    assert "Git tags discovered: 1" in rendered
    assert "Git author captured: 1" in rendered
    assert "Git committer captured: 1" in rendered
    assert "Git tagger captured: 1" in rendered
    _assert_lean_success_output(rendered)
    assert "Coverage details:" in rendered
    assert "Details\n" not in rendered
    assert "Git reflog entries discovered:" not in rendered
    assert "Git file changes discovered:" not in rendered
    assert "filesystem entries discovered:" not in rendered


def test_strict_filesystem_partial_run_renders_useful_data_then_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    file_path = root / "work.txt"
    file_path.write_text("work", encoding="utf-8")
    _set_times(file_path, datetime(2026, 8, 3, 10, 5, tzinfo=UTC))
    missing = tmp_path / "missing"

    output = StringIO()
    errors = StringIO()
    options = parse_options(
        [
            str(root),
            str(missing),
            "--mode",
            "fs",
            "--fs-times",
            "modified",
            "--include-ignored",
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
        r"^Coverage\s+partial \(1 collection error\(s\)\)",
        output.getvalue(),
        re.MULTILINE,
    )
    assert "filesystem metadata could not be read" in errors.getvalue()


def test_verbose_enables_expanded_coverage_without_coverage_flag(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    output = StringIO()
    options = parse_options([str(repo.path), "--time", "all", "--timezone", "UTC", "--verbose", "--no-color"])

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert "Details\n" in rendered
    assert "Scope:" in rendered
    assert "Period:" in rendered
    assert "Schedule:" in rendered
    assert "Coverage:" in rendered
    assert "Coverage details:" in rendered
    assert "timestamp slots requested: 0" in rendered
    assert "not requested=" in rendered


def test_accessed_time_warning_and_non_repository_ignore_policy_are_visible_in_verbose_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    file_path = root / "work.txt"
    file_path.write_text("work", encoding="utf-8")
    _set_times(file_path, datetime(2026, 8, 3, 10, 5, tzinfo=UTC))
    output = StringIO()
    options = parse_options(
        [
            str(root),
            "--mode",
            "fs",
            "--fs-times",
            "accessed",
            "--time",
            "all",
            "--timezone",
            "UTC",
            "--verbose",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert "atime potentially unreliable" in rendered
    assert "outside a Git worktree; no Git ignore rules apply" in rendered


def test_outside_git_file_change_lists_change_kind_path_and_subject(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    repo.commit(
        "work.txt",
        "work",
        "file event subject",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "all",
            "--git-records",
            "file-change",
            "--hours",
            "Mo-Fr 11:00-12:00",
            "--timezone",
            "UTC",
            "--list-outside",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert "Mo-Fr 11:00-12:00" in rendered
    assert "added: work.txt | file event subject" in rendered
