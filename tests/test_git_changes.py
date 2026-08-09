from __future__ import annotations

import os
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from workfold.collectors.git import GitCollector, GitRunner
from workfold.collectors.git_changes import (
    CollectedGitFileChange,
    GitChangeParseError,
    GitFileChangeCollector,
    GitFileChangeRepositoryAccounting,
    parse_diff_tree_name_status,
)
from workfold.models import GitChangeKind, RecordKind, TimestampKind

from support.git_repo import GitRepo


def _identity_environment(timestamp: str) -> dict[str, str]:
    return {
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_AUTHOR_EMAIL": "author@example.test",
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_COMMITTER_DATE": timestamp,
        "GIT_COMMITTER_EMAIL": "committer@example.test",
        "GIT_COMMITTER_NAME": "Fixture Committer",
    }


def _commit_index(repo: GitRepo, subject: str, timestamp: str) -> str:
    repo.run("commit", "-m", subject, environment=_identity_environment(timestamp))
    return repo.run("rev-parse", "HEAD").decode("ascii").strip()


def test_collects_root_modify_delete_and_rename_as_separate_records(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    root = repo.commit(
        "old.txt",
        "one",
        "root",
        author_date="1700000000 +0530",
        committer_date="1700000001 -0230",
    )
    modified = repo.commit(
        "old.txt",
        "two",
        "modify",
        author_date="1700000100 +0530",
        committer_date="1700000101 -0230",
    )
    repo.run("mv", "old.txt", "renamed.txt")
    renamed = repo.commit(
        "renamed.txt",
        "two",
        "rename",
        author_date="1700000200 +0530",
        committer_date="1700000201 -0230",
    )
    repo.run("rm", "renamed.txt")
    deleted = repo.commit(
        "anchor.txt",
        "anchor",
        "delete",
        author_date="1700000300 +0530",
        committer_date="1700000301 -0230",
    )

    commits = GitCollector().collect((repo.path,)).commits
    result = GitFileChangeCollector().collect(commits)

    assert not result.diagnostics
    assert result.requested_commits == 4
    assert result.successful_commits == 4
    assert result.discovered_changes == 5
    assert result.parse_errors == 0
    assert result.subprocess_errors == 0
    (accounting,) = result.repository_accounting
    assert accounting.repository_root == repo.path.resolve()
    assert accounting.requested_commits == 4
    assert accounting.successful_commits == 4
    assert accounting.discovered_changes == 5
    by_commit: dict[str, list[CollectedGitFileChange]] = {}
    for item in result.changes:
        by_commit.setdefault(item.change.commit_id, []).append(item)
    assert [item.change.change_kind for item in by_commit[root]] == [GitChangeKind.ADDED]
    assert [item.change.change_kind for item in by_commit[modified]] == [GitChangeKind.MODIFIED]
    rename_change = by_commit[renamed][0]
    assert rename_change.change.change_kind is GitChangeKind.RENAMED
    assert rename_change.change.raw_status == "R100"
    assert rename_change.change.old_path == Path("old.txt")
    assert rename_change.change.path == Path("renamed.txt")
    assert rename_change.change.similarity == 100
    assert rename_change.diff_basis == modified
    assert {item.change.change_kind for item in by_commit[deleted]} == {
        GitChangeKind.ADDED,
        GitChangeKind.DELETED,
    }
    assert by_commit[root][0].diff_basis == "empty-tree"

    domain = result.to_domain_result((TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER))
    assert len(domain.origins) == 5
    assert len(domain.observations) == 10
    assert all(item.record_kind is RecordKind.GIT_FILE_CHANGE for item in domain.origins)
    rename_origin = next(item for item in domain.origins if item.commit_id == renamed)
    assert rename_origin.path == Path("renamed.txt")
    assert rename_origin.old_path == Path("old.txt")
    assert rename_origin.diff_basis == modified
    rename_observations = [item for item in domain.observations if item.origin.commit_id == renamed]
    assert [item.raw_timestamp for item in rename_observations] == [
        "1700000200 +0530",
        "1700000201 -0230",
    ]
    with pytest.raises(ValueError, match="do not expose"):
        result.to_domain_result((TimestampKind.GIT_TAGGER,))
    with pytest.raises(ValueError, match="support only"):
        rename_change.to_observation(TimestampKind.GIT_TAGGER)


def test_merge_is_compared_only_against_its_first_parent(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "merge")
    (repo.path / "base.txt").write_text("base", encoding="utf-8")
    repo.run("add", "base.txt")
    _commit_index(repo, "base", "1700000000 +0000")

    repo.run("checkout", "-b", "topic")
    (repo.path / "topic.txt").write_text("topic", encoding="utf-8")
    repo.run("add", "topic.txt")
    _commit_index(repo, "topic", "1700000100 +0000")

    repo.run("checkout", "main")
    (repo.path / "main.txt").write_text("main", encoding="utf-8")
    repo.run("add", "main.txt")
    first_parent = _commit_index(repo, "main", "1700000200 +0000")
    repo.run(
        "merge",
        "--no-ff",
        "topic",
        "-m",
        "merge",
        environment=_identity_environment("1700000300 +0000"),
    )
    merge_id = repo.run("rev-parse", "HEAD").decode("ascii").strip()

    commits = GitCollector().collect((repo.path,)).commits
    merge_commit = next(item for item in commits if item.commit.object_id == merge_id)
    result = GitFileChangeCollector().collect((merge_commit,))

    assert not result.diagnostics
    assert [(item.change.change_kind, item.change.path) for item in result.changes] == [
        (GitChangeKind.ADDED, Path("topic.txt"))
    ]
    assert result.changes[0].diff_basis == first_parent


def test_paths_are_nul_safe_and_preserve_undecodable_bytes(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "bytes")
    raw_name = b"tab\tline\ninvalid-\xff.txt"
    raw_repo = os.fsencode(repo.path)
    descriptor = os.open(os.path.join(raw_repo, raw_name), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, b"content")
    finally:
        os.close(descriptor)
    repo.run("add", "--", os.fsdecode(raw_name))
    commit_id = _commit_index(repo, "odd path", "1700000000 +0000")

    commits = GitCollector().collect((repo.path,)).commits
    result = GitFileChangeCollector().collect(commits)

    assert not result.diagnostics
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.change.commit_id == commit_id
    assert change.change.raw_path == raw_name
    assert os.fsencode(change.change.path) == raw_name
    assert os.fsencode(change.to_origin().path or Path()) == raw_name


def test_batches_all_commits_for_a_repository_in_one_diff_process(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "batch")
    repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    repo.commit(
        "two.txt",
        "two",
        "two",
        author_date="1700000100 +0000",
        committer_date="1700000100 +0000",
    )
    commits = GitCollector().collect((repo.path,)).commits

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, ...]] = []

        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls.append(tuple(arguments))
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    runner = RecordingRunner()
    result = GitFileChangeCollector(runner).collect(commits)

    assert result.discovered_changes == 2
    assert [call[0] for call in runner.calls] == ["diff-tree"]
    assert runner.calls[0][1:3] == ("--stdin", "--root")


def test_repository_accounting_includes_empty_repositories_and_validates_partitions(
    tmp_path: Path,
) -> None:
    populated = GitRepo.create(tmp_path / "populated")
    populated.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    empty = GitRepo.create(tmp_path / "empty")
    commit_result = GitCollector().collect((populated.path, empty.path))
    repositories = tuple(item.repository for item in commit_result.repository_accounting)

    result = GitFileChangeCollector().collect(
        commit_result.commits,
        repositories=repositories,
    )

    assert len(result.repository_accounting) == 2
    by_root = {item.repository_root: item for item in result.repository_accounting}
    assert by_root[populated.path.resolve()].requested_commits == 1
    assert by_root[populated.path.resolve()].discovered_changes == 1
    assert by_root[empty.path.resolve()].requested_commits == 0
    assert by_root[empty.path.resolve()].discovered_changes == 0

    valid = GitFileChangeRepositoryAccounting(
        repository=repositories[0],
        requested_commits=1,
        successful_commits=1,
        parse_errors=0,
        subprocess_errors=0,
        discovered_changes=2,
    )
    with pytest.raises(ValueError, match="non-negative"):
        replace(valid, subprocess_errors=-1)
    with pytest.raises(ValueError, match="does not reconcile"):
        replace(valid, parse_errors=1)


def test_parser_validates_commit_boundaries_statuses_and_similarity() -> None:
    first = "a" * 40
    second = "b" * 40
    payload = first.encode() + b"\0\0\nR087\0old\nname\0new\tname\0" + second.encode() + b"\0\0"
    parsed = parse_diff_tree_name_status(payload, (first, second))
    assert len(parsed) == 1
    assert parsed[0].raw_status == "R087"
    assert parsed[0].similarity == 87
    assert parsed[0].raw_old_path == b"old\nname"
    assert parsed[0].raw_path == b"new\tname"

    with pytest.raises(GitChangeParseError) as wrong_commit:
        parse_diff_tree_name_status(b"c" * 40 + b"\0\0", (first,))
    assert wrong_commit.value.code == "unexpected_git_change_commit"

    with pytest.raises(GitChangeParseError) as invalid_status:
        parse_diff_tree_name_status(first.encode() + b"\0\0\nR101\0old\0new\0", (first,))
    assert invalid_status.value.code == "invalid_git_change_status"

    with pytest.raises(GitChangeParseError) as truncated:
        parse_diff_tree_name_status(first.encode() + b"\0\0\nR100\0old", (first,))
    assert truncated.value.code == "truncated_git_change"

    with pytest.raises(GitChangeParseError) as invalid_boundary:
        parse_diff_tree_name_status(first.encode() + b"\0not-a-boundary\0", (first,))
    assert invalid_boundary.value.code == "invalid_git_change_boundary"

    with pytest.raises(GitChangeParseError) as malformed_status:
        parse_diff_tree_name_status(first.encode() + b"\0\0\nnot-status\0path\0", (first,))
    assert malformed_status.value.code == "invalid_git_change_status"

    with pytest.raises(GitChangeParseError) as empty_path:
        parse_diff_tree_name_status(first.encode() + b"\0\0\nA\0\0", (first,))
    assert empty_path.value.code == "invalid_git_change_path"

    assert parse_diff_tree_name_status(b"", ()) == ()
    with pytest.raises(GitChangeParseError) as unrequested:
        parse_diff_tree_name_status(b"unexpected", ())
    assert unrequested.value.code == "unexpected_git_change_output"

    copied = parse_diff_tree_name_status(
        first.encode() + b"\0\0\nC050\0source\0copy\0",
        (first,),
    )
    assert copied[0].change_kind is GitChangeKind.OTHER
    assert copied[0].similarity == 50


def test_command_failure_is_structured_for_the_whole_repository_batch(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "failure")
    repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    commits = GitCollector().collect((repo.path,)).commits

    class FailureRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "diff-tree":
                from workfold.collectors.git import GitCommandError

                raise GitCommandError(
                    code="git_command_failed",
                    message="diff failed",
                    command=tuple(arguments),
                    cwd=cwd,
                    stderr=b"repository detail",
                )
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    result = GitFileChangeCollector(FailureRunner()).collect(commits)
    assert not result.changes
    assert result.successful_commits == 0
    assert result.parse_errors == 0
    assert result.subprocess_errors == 1
    (accounting,) = result.repository_accounting
    assert accounting.requested_commits == 1
    assert accounting.successful_commits == 0
    assert accounting.parse_errors == 0
    assert accounting.subprocess_errors == 1
    assert accounting.discovered_changes == 0
    assert result.is_partial
    assert result.diagnostics[0].stage == "git_file_change_discovery"
    assert "repository detail" in result.diagnostics[0].message


def test_parse_failure_accounts_for_every_commit_in_repository_batch(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "parse-failure")
    repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    repo.commit(
        "two.txt",
        "two",
        "two",
        author_date="1700000100 +0000",
        committer_date="1700000100 +0000",
    )
    commits = GitCollector().collect((repo.path,)).commits

    class MalformedRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(arguments, 0, stdout=b"malformed", stderr=b"")

    result = GitFileChangeCollector(MalformedRunner()).collect(commits)

    assert result.requested_commits == 2
    assert result.successful_commits == 0
    assert result.parse_errors == 2
    assert result.subprocess_errors == 0
    (accounting,) = result.repository_accounting
    assert accounting.requested_commits == 2
    assert accounting.successful_commits == 0
    assert accounting.parse_errors == 2
    assert accounting.subprocess_errors == 0
    assert accounting.discovered_changes == 0
    assert result.diagnostics[0].code == "unexpected_git_change_commit"
    assert result.diagnostics[0].provenance_id is not None
