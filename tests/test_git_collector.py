from __future__ import annotations

import os
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from workfold.collectors.git import (
    GitCollector,
    GitCommandError,
    GitCommitRepositoryAccounting,
    GitRepository,
    GitRepositoryResolver,
    GitRunner,
    RefScope,
    parse_commit_ids,
    resolve_repository,
)
from workfold.models import TimestampKind

from support.git_repo import GitRepo


def test_git_runner_uses_no_shell_and_disables_remote_interaction(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def process(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(arguments), kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"true\n", stderr=b"")

    runner = GitRunner(
        process_runner=process,
        base_environment={
            "PATH": "/fixture/bin",
            "GIT_DIR": "/unrelated/repository",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "malicious",
            "GIT_TRACE": "1",
        },
    )
    runner.run(("rev-parse", "--is-bare-repository"), cwd=tmp_path)

    command, options = calls[0]
    assert options["shell"] is False
    assert options["check"] is False
    assert command[0] == "git"
    assert "protocol.allow=never" in command
    assert options["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert options["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["LC_ALL"] == "C"
    assert "GIT_DIR" not in options["env"]
    assert "GIT_CONFIG_COUNT" not in options["env"]
    assert "GIT_CONFIG_KEY_0" not in options["env"]
    assert "GIT_CONFIG_VALUE_0" not in options["env"]
    assert "GIT_TRACE" not in options["env"]


def test_git_runner_rejects_network_commands_without_spawning(tmp_path: Path) -> None:
    called = False

    def process(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        raise AssertionError("must not spawn")

    runner = GitRunner(process_runner=process)
    with pytest.raises(GitCommandError) as error:
        runner.run(("fetch", "origin"), cwd=tmp_path)

    assert error.value.code == "unsafe_git_command"
    assert not called

    with pytest.raises(GitCommandError) as empty:
        runner.run((), cwd=tmp_path)
    assert empty.value.code == "unsafe_git_command"

    with pytest.raises(GitCommandError) as nul:
        runner.run(("rev-parse", "bad\0argument"), cwd=tmp_path)
    assert nul.value.code == "unsafe_git_argument"


def test_git_runner_validates_limits_and_structures_spawn_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        GitRunner(stderr_limit=-1)

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("git", 1, stderr=b"timeout details")

    with pytest.raises(GitCommandError) as timed_out:
        GitRunner(process_runner=timeout, stderr_limit=7).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert timed_out.value.code == "git_command_timeout"
    assert timed_out.value.stderr == b"timeout"

    def os_error(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("cannot spawn")

    with pytest.raises(GitCommandError) as spawn_error:
        GitRunner(process_runner=os_error).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert spawn_error.value.code == "git_spawn_error"


def test_git_runner_reports_missing_executable_and_bounds_stderr(tmp_path: Path) -> None:
    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    with pytest.raises(GitCommandError) as not_found:
        GitRunner(process_runner=missing).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert not_found.value.code == "git_not_found"
    assert not_found.value.hint == "Install Git or use --mode fs."

    def failed(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 128, stdout=b"", stderr=b"abcdefgh")

    with pytest.raises(GitCommandError) as failure:
        GitRunner(process_runner=failed, stderr_limit=4).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert failure.value.stderr == b"abcd"
    assert failure.value.stderr_text == "abcd…"


def test_collector_resolves_whole_repository_and_preserves_raw_dates(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo with spâce")
    commit_id = repo.commit(
        "nested/work.txt",
        "one",
        "first subject",
        author_date="1704067200 +0530",
        committer_date="1704070800 -0230",
        author_name="Ada Üser",
        author_email="ada@example.test",
    )

    result = GitCollector().collect((repo.path / "nested" / "work.txt", repo.path), ref_scope=RefScope.ALL)

    assert not result.diagnostics
    assert result.requested_targets == 2
    assert result.duplicate_targets == 1
    assert result.successful_repositories == 1
    assert len(result.repositories) == 1
    assert result.repositories[0].root == repo.path.resolve()
    assert result.discovered_commit_ids == 1
    assert result.commits[0].commit.object_id == commit_id
    assert result.commits[0].commit.author.identity.name == "Ada Üser"
    assert result.commits[0].commit.author.raw_timestamp == "1704067200 +0530"
    assert result.commits[0].commit.committer.raw_timestamp == "1704070800 -0230"
    assert result.commits[0].commit.subject == "first subject"
    assert not result.is_partial
    (accounting,) = result.repository_accounting
    assert accounting.repository_root == repo.path.resolve()
    assert accounting.repository_identity == result.repositories[0].identity
    assert accounting.discovered_commit_ids == 1
    assert accounting.captured_commits == accounting.eligible_commits == 1
    assert accounting.record_errors == 0
    assert accounting.operational_errors == 0
    assert accounting.successful
    with pytest.raises(FrozenInstanceError):
        setattr(accounting, "captured_commits", 2)

    domain = result.to_domain_result((TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER))
    assert len(domain.origins) == 1
    assert [item.kind for item in domain.observations] == [
        TimestampKind.GIT_AUTHOR,
        TimestampKind.GIT_COMMITTER,
    ]
    assert domain.observations[0].instant_utc_ns == 1_704_067_200_000_000_000
    assert domain.observations[0].raw_timestamp == "1704067200 +0530"
    assert domain.observations[0].original_offset_minutes == 330
    assert domain.observations[1].actor_name == "Fixture Committer"
    with pytest.raises(ValueError, match="do not expose"):
        result.to_domain_result((TimestampKind.GIT_TAGGER,))
    with pytest.raises(ValueError, match="only Git author"):
        result.commits[0].to_observation(TimestampKind.GIT_REFLOG)


def test_collector_preserves_repository_paths_containing_newlines(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo\nwith-newline")
    commit_id = repo.commit(
        "work.txt",
        "work",
        "newline root",
        author_date="1704067200 +0000",
        committer_date="1704067200 +0000",
    )

    result = GitCollector().collect((repo.path,))

    assert not result.diagnostics
    assert result.repositories[0].root == repo.path.resolve()
    assert result.commits[0].commit.object_id == commit_id


def test_repository_only_resolution_deduplicates_without_traversing_history(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    repo.commit(
        "nested/one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    missing = tmp_path / "missing"

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__()
            self.commands: list[str] = []

        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            self.commands.append(arguments[0])
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    runner = RecordingRunner()
    result = GitRepositoryResolver(runner).resolve((repo.path, repo.path / "nested" / "one.txt", missing))

    assert result.requested_targets == 3
    assert result.successful_targets == 2
    assert result.duplicate_targets == 1
    assert len(result.repositories) == 1
    assert result.is_partial
    assert [item.code for item in result.diagnostics] == ["path_not_found"]
    assert "rev-list" not in runner.commands
    assert "cat-file" not in runner.commands

    complete = GitRepositoryResolver().resolve((repo.path, repo.path))
    assert complete.successful_targets == 2
    assert not complete.is_partial


def test_linked_worktree_contexts_are_retained_but_shared_history_is_collected_once(tmp_path: Path) -> None:
    primary = GitRepo.create(tmp_path / "primary")
    commit_id = primary.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    linked_path = tmp_path / "linked"
    primary.run("worktree", "add", "-b", "linked", str(linked_path))

    resolution = GitRepositoryResolver().resolve((primary.path, linked_path, linked_path / "one.txt"))

    assert resolution.successful_targets == 3
    assert resolution.duplicate_targets == 1
    assert len(resolution.repositories) == 2
    assert len({item.context_identity for item in resolution.repositories}) == 2
    assert len({item.identity for item in resolution.repositories}) == 1

    result = GitCollector().collect((primary.path, linked_path))

    assert {item.root for item in result.repositories} == {primary.path.resolve(), linked_path.resolve()}
    assert [item.commit.object_id for item in result.commits] == [commit_id]
    assert result.discovered_commit_ids == 1
    assert len(result.repository_accounting) == 1
    assert result.repository_accounting[0].repository.root == primary.path.resolve()


def test_commit_repository_accounting_rejects_invalid_partitions(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)
    valid = GitCommitRepositoryAccounting(
        repository=repository,
        discovered_commit_ids=1,
        captured_commits=1,
        record_errors=0,
        duplicate_commit_ids=0,
        unavailable_objects=0,
        parse_errors=0,
        operational_errors=0,
        successful=True,
    )

    with pytest.raises(ValueError, match="non-negative"):
        replace(valid, parse_errors=-1)
    with pytest.raises(ValueError, match="does not reconcile"):
        replace(valid, record_errors=1)


def test_collector_ignores_git_replace_objects_to_preserve_object_provenance(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    original = repo.commit(
        "work.txt",
        "original",
        "original subject",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    replacement = repo.commit(
        "work.txt",
        "replacement",
        "replacement subject",
        author_date="1800000000 +0000",
        committer_date="1800000000 +0000",
        parent=original,
    )
    repo.run("replace", original, replacement)

    result = GitCollector().collect((repo.path,))
    by_id = {item.commit.object_id: item.commit for item in result.commits}

    assert by_id[original].subject == "original subject"
    assert by_id[original].author.epoch_seconds == 1_700_000_000
    assert by_id[replacement].subject == "replacement subject"


def test_collector_ignores_inherited_repository_selection_environment(tmp_path: Path) -> None:
    selected = GitRepo.create(tmp_path / "selected")
    selected_id = selected.commit(
        "selected.txt",
        "selected",
        "selected repository",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    unrelated = GitRepo.create(tmp_path / "unrelated")
    unrelated.commit(
        "unrelated.txt",
        "unrelated",
        "unrelated repository",
        author_date="1800000000 +0000",
        committer_date="1800000000 +0000",
    )
    environment = dict(os.environ)
    environment["GIT_DIR"] = str(unrelated.path / ".git")

    result = GitCollector(GitRunner(base_environment=environment)).collect((selected.path,))

    assert [item.commit.object_id for item in result.commits] == [selected_id]
    assert result.repositories[0].root == selected.path.resolve()


def test_all_refs_includes_non_current_work_and_deduplicates_shared_commits(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    root = repo.commit(
        "root.txt",
        "root",
        "root",
        author_date="1700000000 +0000",
        committer_date="1700000001 +0000",
    )
    main = repo.commit(
        "main.txt",
        "main",
        "main",
        author_date="1700000100 +0000",
        committer_date="1700000101 +0000",
        parent=root,
    )
    topic = repo.commit(
        "topic.txt",
        "topic",
        "topic",
        author_date="1700000200 +0000",
        committer_date="1700000201 +0000",
        parent=root,
        update_ref="refs/heads/topic",
    )
    repo.point_ref("refs/tags/shared", root)

    head_result = GitCollector().collect((repo.path,), ref_scope=RefScope.HEAD)
    all_result = GitCollector().collect((repo.path,), ref_scope=RefScope.ALL)

    assert {item.commit.object_id for item in head_result.commits} == {root, main}
    assert {item.commit.object_id for item in all_result.commits} == {root, main, topic}
    assert all_result.discovered_commit_ids == 3


def test_unborn_and_detached_head_are_valid_scopes(tmp_path: Path) -> None:
    unborn = GitRepo.create(tmp_path / "unborn")
    empty = GitCollector().collect((unborn.path,), ref_scope=RefScope.HEAD)
    assert not empty.diagnostics
    assert empty.successful_repositories == 1
    assert not empty.commits

    repo = GitRepo.create(tmp_path / "detached")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "detached",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    repo.detach(commit_id)
    detached = GitCollector().collect((repo.path,), ref_scope=RefScope.HEAD)
    assert [item.commit.object_id for item in detached.commits] == [commit_id]


def test_collector_returns_actionable_structured_errors_and_continues(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    non_repository = tmp_path / "plain"
    non_repository.mkdir()

    result = GitCollector().collect((non_repository, repo.path, tmp_path / "missing"))

    assert len(result.commits) == 1
    assert result.successful_repositories == 1
    assert {diagnostic.code for diagnostic in result.diagnostics} == {"not_git_repository", "path_not_found"}
    non_repo = next(item for item in result.diagnostics if item.code == "not_git_repository")
    assert non_repo.stage == "git_repository_resolution"
    assert non_repo.hint == "Use --mode fs or pass a path inside a Git repository."
    assert result.is_partial


def test_collector_never_adds_date_pruning_to_git_traversal(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    repo.commit(
        "one.txt",
        "one",
        "crosses any conceivable selection boundary",
        author_date="@1 +1400",
        committer_date="@2000000000 -1200",
    )

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__()
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
    result = GitCollector(runner).collect((repo.path,))

    assert len(result.commits) == 1
    traversal = next(arguments for arguments in runner.arguments if arguments[0] == "rev-list")
    assert traversal == ("rev-list", "--all")
    assert not any("since" in argument or "until" in argument for argument in traversal)


def test_bare_repository_is_a_supported_whole_repository_target(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "source")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "bare",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    bare_path = tmp_path / "bare.git"
    repo.run("clone", "--bare", str(repo.path), str(bare_path))

    result = GitCollector().collect((bare_path,))

    assert not result.diagnostics
    assert result.repositories[0].is_bare
    assert result.repositories[0].root == bare_path.resolve()
    assert [item.commit.object_id for item in result.commits] == [commit_id]


def test_rev_list_parser_validates_and_deduplicates_object_ids(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)
    first = "a" * 40
    second = "b" * 40

    parsed, duplicate_count = parse_commit_ids(
        f"{first}\n{second}\n{first}\n".encode(),
        repository=repository,
    )
    assert parsed == (first, second)
    assert duplicate_count == 1

    with pytest.raises(GitCommandError, match="non-ASCII"):
        parse_commit_ids(b"\xff\n", repository=repository)
    with pytest.raises(GitCommandError, match="invalid object ID"):
        parse_commit_ids(b"nope\n", repository=repository)


def test_repository_resolution_validates_every_git_response(tmp_path: Path) -> None:
    class ResponseRunner(GitRunner):
        def __init__(self, responses: list[bytes | GitCommandError]) -> None:
            self.responses = responses

        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            response = self.responses.pop(0)
            if isinstance(response, GitCommandError):
                raise response
            return subprocess.CompletedProcess(arguments, 0, stdout=response, stderr=b"")

    with pytest.raises(GitCommandError) as invalid_bare:
        resolve_repository(tmp_path, ResponseRunner([b"perhaps\n"]))
    assert invalid_bare.value.code == "invalid_git_output"

    command_failure = GitCommandError(
        code="git_not_found",
        message="missing",
        command=("git",),
        cwd=tmp_path,
    )
    with pytest.raises(GitCommandError) as propagated:
        resolve_repository(tmp_path, ResponseRunner([command_failure]))
    assert propagated.value is command_failure

    later_failure = GitCommandError(
        code="git_command_failed",
        message="failed later",
        command=("git",),
        cwd=tmp_path,
    )
    with pytest.raises(GitCommandError) as later:
        resolve_repository(tmp_path, ResponseRunner([b"false\n", later_failure]))
    assert later.value is later_failure

    with pytest.raises(GitCommandError) as invalid_path:
        resolve_repository(tmp_path, ResponseRunner([b"false\n", b"bad\0path\n"]))
    assert invalid_path.value.code == "invalid_git_output"


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("discovery", "git_command_failed"),
        ("cat-file-command", "git_command_failed"),
        ("batch-envelope", "truncated_cat_file_batch"),
        ("missing", "git_object_unavailable"),
        ("wrong-type", "git_object_not_commit"),
        ("malformed-commit", "invalid_commit_object"),
    ],
)
def test_collector_accounts_for_discovery_and_object_failures(
    tmp_path: Path,
    fault: str,
    expected_code: str,
) -> None:
    repo = GitRepo.create(tmp_path / fault)
    repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )

    class FaultRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if fault == "discovery" and tuple(arguments) == ("rev-list", "--all"):
                raise GitCommandError(
                    code="git_command_failed",
                    message="discovery failed",
                    command=tuple(arguments),
                    cwd=cwd,
                )
            if arguments[0] != "cat-file":
                return super().run(
                    arguments,
                    cwd=cwd,
                    input_data=input_data,
                    allowed_returncodes=allowed_returncodes,
                )
            assert input_data is not None
            object_id = input_data.decode("ascii").strip()
            if fault == "cat-file-command":
                raise GitCommandError(
                    code="git_command_failed",
                    message="object read failed",
                    command=tuple(arguments),
                    cwd=cwd,
                )
            if fault == "batch-envelope":
                output = b"broken"
            elif fault == "missing":
                output = f"{object_id} missing\n".encode()
            elif fault == "wrong-type":
                output = f"{object_id} blob 0\n\n".encode()
            else:
                malformed = b"tree only-no-boundary"
                output = f"{object_id} commit {len(malformed)}\n".encode() + malformed + b"\n"
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

    result = GitCollector(FaultRunner()).collect((repo.path,))

    assert [item.code for item in result.diagnostics] == [expected_code]
    assert not result.commits
    (accounting,) = result.repository_accounting
    assert accounting.discovered_commit_ids == (0 if fault == "discovery" else 1)
    assert accounting.captured_commits == 0
    assert accounting.record_errors == accounting.discovered_commit_ids
    assert accounting.operational_errors == 1
    assert accounting.successful is (fault in {"missing", "wrong-type", "malformed-commit"})
    if fault == "missing":
        assert result.unavailable_objects == 1
    elif fault in {"batch-envelope", "wrong-type", "malformed-commit"}:
        assert result.parse_errors == 1
