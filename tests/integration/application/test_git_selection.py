from __future__ import annotations

import re
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from wuf.application.collection import CollectorServices
from wuf.cli import parse_options
from wuf.cli.runner import default_collector_services, run
from wuf.collection.git import GitCollector, GitRunner

from support.git_repo import GitRepo

BERLIN = ZoneInfo("Europe/Berlin")


def _git_date(value: datetime) -> str:
    offset = value.strftime("%z")
    return f"@{int(value.timestamp())} {offset}"


def _assert_summary_count(rendered: str, label: str, count: int) -> None:
    assert re.search(rf"^{re.escape(label)}\s+{count:,}$", rendered, re.MULTILINE)


def _with_git_collector(collector: GitCollector) -> CollectorServices:
    services = default_collector_services()
    return replace(services, git=replace(services.git, commits=collector))


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
    assert "git:commit:author selected: 0; examined=1, values read=1" in rendered
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
    assert "git:commit:author selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered
    assert "git:commit:committer selected: 0; examined=1, values read=1" in rendered


def test_bounded_git_identity_filter_matches_only_in_range_identity(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    instant = datetime(2026, 8, 3, 10, 0, tzinfo=BERLIN)
    repo.commit(
        "work.txt",
        "one",
        "selected author identity",
        author_date=_git_date(instant),
        committer_date=_git_date(instant),
        author_name="Ada Üser",
        author_email="ADA@EXAMPLE.TEST",
    )

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__(stream_output=False)
            self.arguments: list[tuple[str, ...]] = []

        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            self.arguments.append(tuple(arguments))
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    runner = RecordingRunner()
    output = StringIO()
    options = parse_options(
        [
            str(repo.path),
            "--time",
            "2026-W32",
            "--timezone",
            "Europe/Berlin",
            "--git-identity",
            "üser",
            "--coverage",
            "--verbose",
            "--no-color",
        ]
    )

    assert (
        run(
            options,
            stdout=output,
            stderr=StringIO(),
            terminal_width=100,
            collectors=_with_git_collector(GitCollector(runner)),
        )
        == 0
    )

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 1)
    assert "git:commit:author selected: 1; examined=1, values read=1, scope matches=1, markers=1" in rendered
    traversal = next(arguments for arguments in runner.arguments if arguments[0] == "rev-list")
    assert traversal[3] == "--format=%H%x00%at%x00"
    assert "%an" not in traversal[3]
    assert any(arguments[0] == "cat-file" for arguments in runner.arguments)


@pytest.mark.parametrize(
    ("identity_arguments", "coverage_detail"),
    [
        ((), "scope matches=1, materialization errors=1"),
        (("--git-identity", "fixture author"), "scope errors=1"),
    ],
)
def test_selected_git_hydration_failure_preserves_scope_accounting(
    tmp_path: Path,
    identity_arguments: tuple[str, ...],
    coverage_detail: str,
) -> None:
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
            *identity_arguments,
        ]
    )

    assert (
        run(
            options,
            stdout=output,
            stderr=errors,
            terminal_width=500,
            collectors=_with_git_collector(GitCollector(MissingObjectRunner(stream_output=False))),
        )
        == 0
    )

    rendered = output.getvalue()
    _assert_summary_count(rendered, "Events", 0)
    assert "Coverage  partial · 1 collection error" in rendered
    assert f"git:commit:author selected: 0; examined=1, values read=1, {coverage_detail}" in rendered
    target_timestamps = next(line for line in rendered.splitlines() if "target timestamps [git]" in line)
    assert f"scope errors={int(bool(identity_arguments))}" in target_timestamps
    assert "Git object is unavailable" in errors.getvalue()
