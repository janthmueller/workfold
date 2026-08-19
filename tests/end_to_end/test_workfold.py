"""End-to-end tests for collector composition and collection profiles."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest
from workfold.cli import parse_options
from workfold.cli.runner import default_collector_services, run
from workfold.collection.filesystem import FilesystemCollector
from workfold.collection.filesystem.ignore import (
    GitFilesystemInventory,
    GitFilesystemInventoryBackend,
    GitIgnoreCommandError,
    GitIgnoreProbe,
    GitIgnoreRepository,
    GitIgnoreRunner,
    GitIgnoreService,
)

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
    _assert_no_verbose_output(rendered)
    assert not re.search(r"^Coverage\s{2,}", rendered, re.MULTILINE)


def _assert_no_verbose_output(rendered: str) -> None:
    assert not re.search(r"^(?:Scope|Period|Breakdown)\b", rendered, re.MULTILINE)
    assert "complete for all discoverable timestamps" not in rendered


class _IncompleteInventoryService(GitIgnoreService):
    def __init__(self, repository: GitIgnoreRepository) -> None:
        self._repository = repository

        def incomplete_inventory(
            _runner: GitIgnoreRunner,
            selected_repository: GitIgnoreRepository,
            selected_root: Path,
        ) -> GitFilesystemInventory:
            assert selected_repository == self._repository
            warning = GitIgnoreCommandError(
                code="git_filesystem_inventory_incomplete",
                message="could not open directory 'private/': Permission denied",
                cwd=selected_root,
                command=("ls-files",),
            )
            return GitFilesystemInventory(included_relative_paths=("work.txt",), warning=warning)

        super().__init__(
            inventory_backend=GitFilesystemInventoryBackend.materialized(incomplete_inventory),
        )

    def probe(self, path: Path, *, is_directory: bool) -> GitIgnoreProbe:
        del path, is_directory
        return GitIgnoreProbe(self._repository, True, "test repository")


def test_default_profile_uses_local_branches_while_all_refs_includes_remote_tracking(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    root = repo.commit(
        "root.txt",
        "root",
        "root",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
    )
    repo.commit(
        "topic.txt",
        "topic",
        "local topic",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        parent=root,
        update_ref="refs/heads/topic",
    )
    repo.commit(
        "remote.txt",
        "remote",
        "remote tracking only",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        parent=root,
        update_ref="refs/remotes/origin/remote-only",
    )

    default_output = StringIO()
    default_options = parse_options([str(repo.path), "--time", "all", "--no-color"])
    assert run(default_options, stdout=default_output, stderr=StringIO(), terminal_width=80) == 0

    all_refs_output = StringIO()
    all_refs = parse_options([str(repo.path), "--time", "all", "--git-commits-from", "all-refs", "--no-color"])
    assert run(all_refs, stdout=all_refs_output, stderr=StringIO(), terminal_width=80) == 0

    _assert_summary_count(default_output.getvalue(), "Events", 2)
    _assert_summary_count(all_refs_output.getvalue(), "Events", 3)


def test_filesystem_profile_flows_through_the_shared_pipeline(tmp_path: Path) -> None:
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
            "--events",
            "fs:file:modified",
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
    assert "fs:file:modified selected: 1" in rendered
    assert "Coverage details:" in rendered
    assert "Details\n" not in rendered


def test_visual_count_grouping_flows_from_cli_to_busy_filesystem_cells(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    instant = datetime(2026, 8, 3, 10, 5, tzinfo=UTC)
    for index in range(15):
        path = root / f"work-{index:02d}.txt"
        path.write_text("work", encoding="utf-8")
        _set_times(path, instant)

    base_arguments = [
        str(root),
        "--events",
        "fs:file:modified",
        "fs:file:accessed",
        "--include-ignored",
        "--time",
        "all",
        "--timezone",
        "UTC",
        "--hours",
        "all",
        "--no-color",
    ]
    per_event_output = StringIO()
    per_visual_output = StringIO()

    assert (
        run(
            parse_options(base_arguments),
            stdout=per_event_output,
            stderr=StringIO(),
            terminal_width=80,
        )
        == 0
    )
    assert (
        run(
            parse_options([*base_arguments, "--count-grouping", "visual"]),
            stdout=per_visual_output,
            stderr=StringIO(),
            terminal_width=80,
        )
        == 0
    )

    per_event = per_event_output.getvalue()
    per_visual = per_visual_output.getvalue()
    assert "■×15 ■×15" in per_event
    assert "×N exact per event kind" in per_event
    assert "■×30" in per_visual
    assert "×N exact per visual" in per_visual
    _assert_summary_count(per_event, "Events", 30)
    _assert_summary_count(per_visual, "Events", 30)


def test_all_hours_and_midnight_start_labels_flow_through_the_cli(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    file_path = root / "weekend.txt"
    file_path.write_text("work", encoding="utf-8")
    _set_times(file_path, datetime(2026, 8, 9, 23, 30, tzinfo=UTC))
    output = StringIO()
    options = parse_options(
        [
            str(root),
            "--events",
            "fs:file:modified",
            "--include-ignored",
            "--time",
            "all",
            "--timezone",
            "UTC",
            "--hours",
            "all",
            "--cluster-anchor",
            "midnight",
            "--band-label",
            "start",
            "--show-empty-bands",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert rendered.startswith("Time")
    assert not rendered.startswith("Time (")
    assert re.search(r"^23:00\s", rendered, re.MULTILINE)
    assert re.search(r"^22:00\s*$", rendered, re.MULTILINE)
    assert "23:30" not in rendered
    assert "Working hours: all" in rendered
    assert "Outside working hours" not in rendered
    assert re.search(r"^Schedule\s+1 inside \(100\.0%\) · 0 outside \(0\.0%\)$", rendered, re.MULTILINE)
    assert re.search(r"^Calendar\s+0 weekday \(0\.0%\) · 1 weekend \(100\.0%\)$", rendered, re.MULTILINE)


@pytest.mark.parametrize("force_directory_aware", [False, True])
def test_incomplete_filesystem_inventory_is_a_clean_warning_unless_strict(
    tmp_path: Path,
    *,
    force_directory_aware: bool,
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    (repo.path / "work.txt").write_text("work", encoding="utf-8")
    repository = GitIgnoreRepository(repo.path.resolve(), False)
    ignore_service = _IncompleteInventoryService(repository)
    collector = (
        FilesystemCollector(ignore_service=ignore_service, lstat_reader=os.lstat)
        if force_directory_aware
        else FilesystemCollector(ignore_service=ignore_service)
    )
    arguments = [
        str(repo.path),
        "--events",
        "fs:file:modified",
        "--time",
        "all",
        "--timezone",
        "UTC",
        "--no-color",
    ]

    output = StringIO()
    diagnostics = StringIO()
    status = run(
        parse_options(arguments),
        stdout=output,
        stderr=diagnostics,
        terminal_width=80,
        collectors=replace(default_collector_services(), filesystem=collector),
    )

    assert status == 0
    assert re.search(r"^Coverage\s+partial · filesystem inventory incomplete$", output.getvalue(), re.MULTILINE)
    assert diagnostics.getvalue().startswith("\nwarning: could not open directory 'private/': Permission denied")
    assert "\nworkfold:" not in diagnostics.getvalue()
    assert "warning: warning:" not in diagnostics.getvalue()
    assert "\nhint:" not in diagnostics.getvalue()

    strict_diagnostics = StringIO()
    strict_status = run(
        parse_options([*arguments, "--strict"]),
        stdout=StringIO(),
        stderr=strict_diagnostics,
        terminal_width=80,
        collectors=replace(default_collector_services(), filesystem=collector),
    )

    assert strict_status == 1
    assert strict_diagnostics.getvalue().startswith("\nerror: could not open directory 'private/': Permission denied")


def test_filesystem_entry_selection_is_honored_by_the_application(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "work.txt").write_text("work", encoding="utf-8")
    output = StringIO()
    options = parse_options(
        [
            str(root),
            "--events",
            "fs:directory:modified",
            "--time",
            "all",
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


def test_directory_coverage_discloses_pruned_ignored_subtrees(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    nested = repo.path / "ignored" / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "artifact.txt").write_text("ignored", encoding="utf-8")
    (repo.path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--events",
            "fs:directory:modified",
            "--time",
            "all",
            "--coverage",
            "--timezone",
            "UTC",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=100) == 0

    normalized = " ".join(output.getvalue().split())
    assert "1 ignored filesystem subtree pruned; descendant directories not counted" in normalized


def test_combined_profile_preserves_git_and_filesystem_as_distinct_evidence(tmp_path: Path) -> None:
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
            "--events",
            "git:commit:author",
            "fs:file:modified",
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


def test_full_profile_keeps_expanded_report_details_opt_in(tmp_path: Path) -> None:
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
    _assert_no_verbose_output(rendered)
    assert "Mo-Fr 08:00-16:30" in rendered
    assert "Coverage details:" not in rendered
    assert "Details\n" not in rendered


def test_full_profile_collects_all_record_families_and_reports_capabilities(
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
            "--profile",
            "full",
            "--coverage",
            "--git-identity",
            "Fixture",
            "--fs-exclude",
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
    assert "git:tag:tagger selected: 1" in rendered
    assert "fs:file:birth selected:" in rendered
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
    assert "git:commit:author selected: 1" in rendered
    assert "git:commit:committer selected: 1" in rendered
    assert "git:tag:tagger selected: 1" in rendered
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
            "--events",
            "fs:file:modified",
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
        r"^Coverage\s+partial · 1 collection error",
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
    assert "timestamp slots examined: 0" in rendered
    assert "scope event kinds: requested=git:commit:author" in rendered


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
            "--events",
            "fs:file:accessed",
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
            "--events",
            "git:file-change:author",
            "--hours",
            "Mo-Fr 11:00-12:00",
            "--timezone",
            "UTC",
            "--list",
            "outside",
            "--no-color",
        ]
    )

    assert run(options, stdout=output, stderr=StringIO(), terminal_width=80) == 0

    rendered = output.getvalue()
    assert "Mo-Fr 11:00-12:00" in rendered
    assert "added: work.txt | file event subject" in rendered
