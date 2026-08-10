from __future__ import annotations

import os
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path

import pytest
import workfold.collectors.git_reflogs as reflog_module
from workfold.collectors.git import GitCollector, GitCommandError, GitRepository, GitRunner
from workfold.collectors.git_reflogs import (
    GitReflogCollector,
    GitReflogParseError,
    GitReflogReadError,
    ReflogRef,
    discover_reflog_names,
    parse_current_refs,
    parse_reflog_entries,
    parse_reflog_list,
    parse_reflog_selectors,
    read_semantic_reflog,
)
from workfold.models import RecordKind, TimestampKind

from support.git_repo import GitRepo


def _reflog_environment(timestamp: str) -> dict[str, str]:
    return {
        "GIT_COMMITTER_DATE": timestamp,
        "GIT_COMMITTER_EMAIL": "operator@example.test",
        "GIT_COMMITTER_NAME": "Reflog Operator",
    }


def _write_reflog_ref(
    repo: GitRepo,
    ref_name: str,
    new_id: str,
    *,
    timestamp: str,
    message: str,
    old_id: str | None = None,
) -> None:
    arguments = ["update-ref", "--create-reflog", "-m", message, ref_name, new_id]
    if old_id is not None:
        arguments.append(old_id)
    repo.run(*arguments, environment=_reflog_environment(timestamp))


def test_collects_all_available_namespaces_with_exact_raw_provenance(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "reflogs")
    first = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    second = repo.commit(
        "two.txt",
        "two",
        "two",
        author_date="1700000100 +0000",
        committer_date="1700000100 +0000",
    )
    _write_reflog_ref(
        repo,
        "refs/custom/activity",
        first,
        timestamp="1701000000 +0545",
        message="custom first",
    )
    _write_reflog_ref(
        repo,
        "refs/custom/activity",
        second,
        old_id=first,
        timestamp="1701000100 -0230",
        message="custom second",
    )
    for ref_name in ("refs/heads/topic", "refs/remotes/origin/main", "refs/stash"):
        _write_reflog_ref(
            repo,
            ref_name,
            second,
            timestamp="1701000200 +0000",
            message=f"create {ref_name}",
        )
    repo.point_ref("refs/tags/no-reflog", second)

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitReflogCollector().collect(repositories)

    assert not result.diagnostics
    assert result.successful_repositories == 1
    assert result.captured_entries == len(result.entries)
    assert result.unavailable_entries == 0
    available_names = {item.ref_name for item in result.available_refs}
    assert {
        "refs/custom/activity",
        "refs/heads/topic",
        "refs/remotes/origin/main",
        "refs/stash",
    } <= available_names
    unavailable_names = {item.ref_name for item in result.refs_without_reflog}
    assert "refs/tags/no-reflog" in unavailable_names

    custom = [item.entry for item in result.entries if item.entry.ref_name == "refs/custom/activity"]
    assert len(custom) == 2
    newest, oldest = custom
    assert newest.new_id == second
    assert newest.old_id == first
    assert newest.raw_timestamp == "1701000100 -0230"
    assert newest.offset_seconds == -(2 * 3_600 + 30 * 60)
    assert newest.raw_selector == "refs/custom/activity@{0}"
    assert newest.actor_name == "Reflog Operator"
    assert newest.actor_email == "operator@example.test"
    assert newest.raw_actor == "Reflog Operator <operator@example.test>"
    assert newest.message == "custom second"
    assert oldest.old_id == "0" * len(first)
    assert oldest.raw_timestamp == "1701000000 +0545"
    assert oldest.raw_selector == "refs/custom/activity@{1}"

    origins = tuple(item.to_origin() for item in result.entries)
    observations = tuple(item.to_observation() for item in result.entries)
    assert len(origins) == len(result.entries)
    assert len(observations) == len(result.entries)
    assert all(item.record_kind is RecordKind.REFLOG for item in origins)
    assert all(item.kind is TimestampKind.GIT_REFLOG for item in observations)
    custom_observation = next(
        item
        for item in observations
        if item.origin.ref_name == "refs/custom/activity" and item.raw_timestamp.endswith("-0230")
    )
    assert custom_observation.original_offset_minutes == -150
    assert custom_observation.origin.object_id == second
    assert custom_observation.origin.target_id == first


@pytest.mark.skipif(os.name == "nt", reason="Windows directory names cannot contain newlines")
def test_collects_reflogs_from_repository_path_containing_newline(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "reflog\nrepository")
    repo.commit(
        "work.txt",
        "work",
        "newline root",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitReflogCollector().collect(repositories)

    assert not result.diagnostics
    assert result.entries
    assert {item.repository.root for item in result.entries} == {repo.path.resolve()}


def test_non_commit_and_mixed_object_reflogs_are_not_silently_dropped(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "mixed")
    first = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    second = repo.commit(
        "two.txt",
        "two",
        "two",
        author_date="1700000100 +0000",
        committer_date="1700000100 +0000",
    )
    blob = repo.run("hash-object", "-w", "--stdin", input_data=b"blob payload").decode("ascii").strip()
    ref_name = "refs/custom/mixed"
    _write_reflog_ref(
        repo,
        ref_name,
        first,
        timestamp="1702000000 +0000",
        message="commit",
    )
    _write_reflog_ref(
        repo,
        ref_name,
        blob,
        old_id=first,
        timestamp="1702000100 +0100",
        message="blob",
    )
    _write_reflog_ref(
        repo,
        ref_name,
        second,
        old_id=blob,
        timestamp="1702000200 -0100",
        message="back to commit",
    )

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitReflogCollector().collect(repositories)

    assert not result.diagnostics
    entries = [item.entry for item in result.entries if item.entry.ref_name == ref_name]
    assert [(item.old_id, item.new_id, item.message) for item in entries] == [
        (blob, second, "back to commit"),
        (first, blob, "blob"),
        ("0" * len(first), first, "commit"),
    ]
    assert [item.raw_timestamp for item in entries] == [
        "1702000200 -0100",
        "1702000100 +0100",
        "1702000000 +0000",
    ]
    status = next(item for item in result.available_refs if item.ref_name == ref_name)
    assert status.entry_count == status.captured_entry_count == 3
    assert status.unavailable_entry_count == 0
    assert result.unavailable_entries == 0


def test_linked_worktree_head_uses_git_resolved_worktree_log_path(tmp_path: Path) -> None:
    primary = GitRepo.create(tmp_path / "primary")
    primary.commit(
        "base.txt",
        "base",
        "base",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    linked_path = tmp_path / "linked"
    primary.run("worktree", "add", "-b", "linked", str(linked_path))
    linked = GitRepo(linked_path, branch="linked")
    (linked.path / "linked.txt").write_text("linked", encoding="utf-8")
    linked.run("add", "linked.txt")
    linked.run(
        "commit",
        "-m",
        "linked commit",
        environment={
            "GIT_AUTHOR_DATE": "1703000000 +0000",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_AUTHOR_NAME": "Linked Author",
            **_reflog_environment("1703000000 +0000"),
        },
    )

    repositories = GitCollector().collect((primary.path, linked_path)).repositories
    assert {item.root for item in repositories} == {primary.path.resolve(), linked_path.resolve()}
    assert len({item.context_identity for item in repositories}) == 2
    assert len({item.identity for item in repositories}) == 1
    linked_repository = next(item for item in repositories if item.root == linked_path.resolve())
    assert linked_repository.git_dir != linked_repository.common_dir
    result = GitReflogCollector().collect(repositories)

    assert not result.diagnostics
    assert result.requested_repositories == 2
    assert result.successful_repositories == 2
    head_statuses = [item for item in result.available_refs if item.ref_name == "HEAD"]
    assert {item.repository.root for item in head_statuses} == {primary.path.resolve(), linked_path.resolve()}
    linked_head_entries = [
        item.entry
        for item in result.entries
        if item.repository.root == linked_path.resolve() and item.entry.ref_name == "HEAD"
    ]
    assert linked_head_entries
    assert linked_head_entries[0].message == "commit: linked commit"
    assert linked_head_entries[0].raw_timestamp == "1703000000 +0000"
    assert any(item.ref_name == "refs/heads/linked" for item in result.available_refs)
    shared_status_names = [item.ref_name for item in result.available_refs if item.ref_name != "HEAD"]
    assert len(shared_status_names) == len(set(shared_status_names))


def test_relative_git_path_is_resolved_from_selected_repository_not_process_cwd(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "selected")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    _write_reflog_ref(
        repo,
        "refs/custom/relative",
        commit_id,
        timestamp="1704000000 +0000",
        message="relative",
    )
    repositories = GitCollector().collect((repo.path,)).repositories

    class RelativePathRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            completed = super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )
            if arguments[0] == "rev-parse" and "--git-path" in arguments:
                assert completed.stdout.endswith(b"\n")
                absolute = Path(os.fsdecode(completed.stdout[:-1]))
                if not absolute.is_absolute():
                    return completed
                relative = absolute.relative_to(cwd)
                return subprocess.CompletedProcess(
                    completed.args,
                    completed.returncode,
                    os.fsencode(relative) + b"\n",
                    completed.stderr,
                )
            return completed

    result = GitReflogCollector(RelativePathRunner()).collect(repositories)
    assert not result.diagnostics
    assert any(item.entry.ref_name == "refs/custom/relative" for item in result.entries)


def test_repository_without_reflogs_reports_absence_without_error(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "empty")
    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitReflogCollector().collect(repositories)

    assert not result.diagnostics
    assert result.successful_repositories == 1
    assert result.discovered_refs == 1
    assert not result.available_refs
    assert [item.ref_name for item in result.refs_without_reflog] == ["HEAD"]
    assert not result.entries


def test_raw_parser_preserves_bytes_selectors_duplicates_and_messages() -> None:
    old_id = b"0" * 40
    new_id = b"a" * 40
    line = old_id + b" " + new_id + b" Name-\xff <mail-\xfe@example.test> 1700000000 +0545\tmessage\twith tab\xff\n"
    entries = parse_reflog_entries(line + line, ref_name="refs/custom/raw")

    assert len(entries) == 2
    newest, oldest = entries
    assert newest.raw_selector == "refs/custom/raw@{0}"
    assert oldest.raw_selector == "refs/custom/raw@{1}"
    assert newest.raw_timestamp_bytes == b"1700000000 +0545"
    assert newest.offset_seconds == 5 * 3_600 + 45 * 60
    assert newest.raw_actor_name == b"Name-\xff"
    assert newest.raw_actor_email == b"mail-\xfe@example.test"
    assert newest.raw_message == b"message\twith tab\xff"
    assert newest.duplicate_ordinal == 1
    assert oldest.duplicate_ordinal == 0
    assert newest.actor_name.encode("utf-8", errors="surrogateescape") == b"Name-\xff"


def test_raw_parser_accepts_empty_log_and_message_without_tab() -> None:
    assert parse_reflog_entries(b"", ref_name="HEAD") == ()
    payload = b"0" * 40 + b" " + b"a" * 40 + b" Person <person@example.test> 1700000000 -0100\n"

    (entry,) = parse_reflog_entries(payload, ref_name="HEAD")

    assert entry.message == ""
    assert entry.raw_message == b""
    assert entry.offset_seconds == -3_600


@pytest.mark.parametrize(
    "payload",
    [
        b"impossible\0record\n",
        b"no-spaces\n",
        b"invalid old-and-new\n",
        b"0" * 40 + b" " + b"a" * 64 + b" Person <p@example.test> 1 +0000\n",
        b"0" * 40 + b" " + b"a" * 40 + b" incomplete-signature\n",
        b"0" * 40 + b" " + b"a" * 40 + b" Person <p@example.test> invalid +0000\n",
        b"0" * 40 + b" " + b"a" * 40 + b" invalid-identity 1 +0000\n",
    ],
)
def test_raw_parser_rejects_each_malformed_record_shape(payload: bytes) -> None:
    with pytest.raises(GitReflogParseError):
        parse_reflog_entries(payload, ref_name="HEAD")


@pytest.mark.parametrize(
    ("timestamp", "expected_code"),
    [
        (b"999999999999 +0000", "invalid_git_reflog_timestamp"),
        (b"1700000000 +2500", "invalid_git_reflog_timestamp"),
        (b"1" * 5_000 + b" +0000", "invalid_git_reflog_timestamp"),
    ],
)
def test_raw_parser_rejects_unlocalizable_or_unrepresentable_timestamps(
    timestamp: bytes,
    expected_code: str,
) -> None:
    payload = b"0" * 40 + b" " + b"a" * 40 + b" Person <person@example.test> " + timestamp + b"\tmessage\n"
    with pytest.raises(GitReflogParseError) as error:
        parse_reflog_entries(payload, ref_name="HEAD")
    assert error.value.code == expected_code


def test_discovery_parsers_validate_refs_and_add_unborn_head() -> None:
    object_id = b"a" * 40
    assert parse_current_refs(b"") == ("HEAD",)
    assert parse_current_refs(object_id + b" refs/heads/main\n") == ("HEAD", "refs/heads/main")
    assert parse_current_refs(object_id + b" HEAD\n" + object_id + b" HEAD\n") == ("HEAD",)
    assert parse_reflog_list(b"HEAD\nrefs/heads/main\nHEAD\n") == ("HEAD", "refs/heads/main")
    assert parse_reflog_selectors(b"HEAD@{0}\nrefs/heads/main@{2}\nHEAD@{1}\n") == (
        "HEAD",
        "refs/heads/main",
    )

    with pytest.raises(GitReflogParseError) as invalid_current:
        parse_current_refs(b"not-an-oid refs/heads/main\n")
    assert invalid_current.value.code == "invalid_git_ref_list"
    with pytest.raises(GitReflogParseError) as nul_current:
        parse_current_refs(object_id + b" refs/heads/bad\0ref\n")
    assert nul_current.value.code == "invalid_git_ref_list"
    with pytest.raises(GitReflogParseError) as invalid_list:
        parse_reflog_list(b"\0bad\n")
    assert invalid_list.value.code == "invalid_git_reflog_list"
    with pytest.raises(GitReflogParseError) as empty_list_entry:
        parse_reflog_list(b"\n")
    assert empty_list_entry.value.code == "invalid_git_reflog_list"
    with pytest.raises(GitReflogParseError) as invalid_selector:
        parse_reflog_selectors(b"not-a-selector\n")
    assert invalid_selector.value.code == "invalid_git_reflog_list"

    with pytest.raises(GitReflogParseError) as truncated:
        parse_reflog_entries(
            b"0" * 40 + b" " + b"a" * 40 + b" Person <person@example.test> 1700000000 +0000\tmessage",
            ref_name="HEAD",
        )
    assert truncated.value.code == "truncated_git_reflog_entry"


def test_reflog_discovery_falls_back_for_git_without_reflog_list(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)

    class LegacyRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if tuple(arguments) == ("reflog", "list"):
                raise GitCommandError(
                    code="git_command_failed",
                    message="legacy Git interpreted list as a ref",
                    command=tuple(arguments),
                    cwd=cwd,
                )
            assert tuple(arguments) == ("reflog", "show", "--all", "--format=%gD")
            return subprocess.CompletedProcess(arguments, 0, b"HEAD@{0}\nrefs/heads/main@{1}\n", b"")

    assert discover_reflog_names(LegacyRunner(), repository) == (
        "HEAD",
        "refs/heads/main",
    )


def test_semantic_reflog_reader_rejects_unsafe_missing_and_non_regular_paths(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / ".git"
    metadata.mkdir()
    repository = GitRepository(tmp_path, metadata, metadata, False)
    valid = metadata / "valid-log"
    valid.write_bytes(b"payload\n")
    assert read_semantic_reflog(valid, repository=repository) == (b"payload\n", False)

    outside = tmp_path / "outside"
    outside.write_bytes(b"payload\n")
    with pytest.raises(GitReflogReadError) as unsafe:
        read_semantic_reflog(outside, repository=repository)
    assert unsafe.value.code == "unsafe_git_reflog_path"
    assert unsafe.value.path == outside

    directory = metadata / "directory"
    directory.mkdir()
    with pytest.raises(GitReflogReadError) as non_regular:
        read_semantic_reflog(directory, repository=repository)
    assert non_regular.value.code == "invalid_git_reflog_file"

    missing = metadata / "missing"
    with pytest.raises(GitReflogReadError) as unreadable:
        read_semantic_reflog(missing, repository=repository)
    assert unreadable.value.code == "git_reflog_read_error"
    assert unreadable.value.path == missing


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow file descriptors")
def test_semantic_reflog_reader_rejects_a_symlink_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = tmp_path / ".git"
    metadata.mkdir()
    repository = GitRepository(tmp_path, metadata, metadata, False)
    reflog = metadata / "log"
    reflog.write_bytes(b"safe\n")
    outside = tmp_path / "outside"
    outside.write_bytes(b"must not be read\n")
    original_resolve = Path.resolve
    swapped = False

    def resolve_and_swap(path: Path, *, strict: bool = False) -> Path:
        nonlocal swapped
        resolved = original_resolve(path, strict=strict)
        if path == reflog and not swapped:
            swapped = True
            reflog.unlink()
            reflog.symlink_to(outside)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_and_swap)

    with pytest.raises(GitReflogReadError) as error:
        read_semantic_reflog(reflog, repository=repository)

    assert error.value.code == "invalid_git_reflog_file"


@pytest.mark.parametrize(
    ("fault", "expected_code", "expected_stage"),
    [
        ("discovery-command", "git_command_failed", "git_reflog_discovery"),
        ("discovery-parse", "invalid_git_ref_list", "git_reflog_discovery"),
        ("path-command", "git_command_failed", "git_reflog_path"),
        ("invalid-path", "invalid_git_reflog_path", "git_reflog_read"),
        ("read-error", "git_reflog_read_error", "git_reflog_read"),
        ("parse-error", "invalid_git_reflog_entry", "git_reflog_parse"),
        (
            "changed-during-read",
            "git_reflog_changed_during_collection",
            "git_reflog_read",
        ),
    ],
)
def test_reflog_collector_accounts_for_discovery_path_read_and_parse_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected_code: str,
    expected_stage: str,
) -> None:
    metadata = tmp_path / ".git"
    reflog_path = metadata / "logs" / "custom"
    reflog_path.parent.mkdir(parents=True)
    object_id = "a" * 40
    valid_payload = b"0" * 40 + b" " + object_id.encode() + b" Person <person@example.test> 1700000000 +0000\tmessage\n"
    reflog_path.write_bytes(b"bad\n" if fault == "parse-error" else valid_payload)
    repository = GitRepository(tmp_path, metadata, metadata, False)

    if fault == "changed-during-read":

        def changed_read(
            path: Path,
            *,
            repository: GitRepository,
        ) -> tuple[bytes, bool]:
            return valid_payload, True

        monkeypatch.setattr(reflog_module, "read_semantic_reflog", changed_read)

    class FaultRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "show-ref":
                if fault == "discovery-command":
                    raise GitCommandError(
                        code="git_command_failed",
                        message="reflog discovery failed",
                        command=tuple(arguments),
                        cwd=cwd,
                        stderr=b"discovery detail",
                    )
                output = b"malformed\n" if fault == "discovery-parse" else (object_id.encode() + b" refs/custom\n")
                return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")
            if arguments[0] == "reflog":
                return subprocess.CompletedProcess(arguments, 0, stdout=b"refs/custom\n", stderr=b"")
            if fault == "path-command":
                raise GitCommandError(
                    code="git_command_failed",
                    message="path lookup failed",
                    command=tuple(arguments),
                    cwd=cwd,
                )
            if fault == "invalid-path":
                output = b"not-newline-terminated"
            elif fault == "read-error":
                output = os.fsencode(metadata / "missing") + b"\n"
            else:
                output = os.fsencode(reflog_path) + b"\n"
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

    result = GitReflogCollector(FaultRunner()).collect((repository,))

    assert result.is_partial
    assert result.successful_repositories == 0
    assert [item.code for item in result.diagnostics] == [expected_code]
    assert result.diagnostics[0].stage == expected_stage
    if fault == "discovery-command":
        assert "discovery detail" in result.diagnostics[0].message
    if fault == "parse-error":
        assert result.parse_errors == 1
        assert result.unavailable_entries == 1
        assert result.available_refs[0].unavailable_entry_count == 1
    if fault == "changed-during-read":
        assert result.captured_entries == 1


def test_reflog_status_never_reports_negative_unavailable_count(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)
    assert ReflogRef(repository, "HEAD", 1, 2).unavailable_entry_count == 0
