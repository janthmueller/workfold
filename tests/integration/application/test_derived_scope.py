from __future__ import annotations

import re
from collections.abc import Collection, Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from workfold.application.collection import collect as collect_sources
from workfold.cli import parse_options
from workfold.cli.runner import default_collector_services, run
from workfold.collection.git import GitCommandError, GitRunner
from workfold.collection.git.changes import GitFileChangeCollector
from workfold.domain.observations import TimestampKind, TimestampObservation
from workfold.domain.scope import ObservationScope
from workfold.domain.time import InstantRange, InstantRangeUnion, datetime_to_utc_ns
from workfold.folding.pipeline import ObservationBatch

from support.git_repo import GitRepo

BERLIN = ZoneInfo("Europe/Berlin")


def _git_date(value: datetime) -> str:
    offset = value.strftime("%z")
    return f"@{int(value.timestamp())} {offset}"


def _assert_summary_count(rendered: str, label: str, count: int) -> None:
    assert re.search(rf"^{re.escape(label)}\s+{count:,}$", rendered, re.MULTILINE)


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

    # Keep the per-target detail on one line even under macOS's longer
    # temporary-directory prefix. Narrow wrapping has dedicated renderer tests.
    assert run(options, stdout=output, stderr=StringIO(), terminal_width=500) == 0

    rendered = output.getvalue()
    assert "Git file changes discovered: 1" in rendered
    assert (
        "Git commit inputs for file-change derivation: reachable=1, examined=1, "
        "candidates=1, hydrated=1, selected=1, scope evaluation errors=0, record errors=0" in rendered
    )
    assert (
        "Git file-change derivation: commits requested=1, successfully parsed=1, "
        "parse failures=0, subprocess failures=0, file changes discovered=1" in rendered
    )
    assert (
        f"target Git commit inputs [git] {repo.path.resolve()}: reachable=1, "
        "examined=1, candidates=1, hydrated=1, selected=1, scope evaluation errors=0" in rendered
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

        @contextmanager
        def open_stdout(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_stream: BinaryIO | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> Generator[BinaryIO, None, None]:
            if arguments[0] == "diff-tree":
                assert input_stream is not None
                self.diff_inputs.append(input_stream.read())
                input_stream.seek(0)
            with super().open_stdout(
                arguments,
                cwd=cwd,
                input_stream=input_stream,
                allowed_returncodes=allowed_returncodes,
            ) as stdout:
                yield stdout

    def collect_observations(
        selected_scope: ObservationScope,
        file_changes: GitFileChangeCollector,
    ) -> tuple[TimestampObservation, ...]:
        observations: list[TimestampObservation] = []

        def consume(batch: ObservationBatch) -> None:
            observations.extend(batch.observations)

        collect_sources(
            options,
            replace(default_collector_services(), file_changes=file_changes),
            observation_consumer=consume,
            observation_scope=selected_scope,
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
            default_collector_services(),
            observation_consumer=consume,
            observation_scope=selected_scope,
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
        @contextmanager
        def open_stdout(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_stream: BinaryIO | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> Generator[BinaryIO, None, None]:
            raise GitCommandError(
                code="git_command_failed",
                message="diff failed",
                command=tuple(arguments),
                cwd=cwd,
            )
            yield BytesIO()

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
            collectors=replace(
                default_collector_services(),
                file_changes=GitFileChangeCollector(FailureRunner()),
            ),
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
