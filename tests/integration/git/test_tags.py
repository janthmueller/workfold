from __future__ import annotations

import subprocess
from collections.abc import Collection, Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from workfold.collection.git import GitCollector, GitCommandError, GitRepository, GitRunner
from workfold.collection.git.tags import (
    CollectedGitTag,
    GitTagCollector,
    GitTagParseError,
    GitTagRepositoryAccounting,
    parse_tag_object,
    parse_tag_refs,
)
from workfold.domain.observations import RecordKind, TimestampKind

from support.git_repo import GitRepo


def _tagger_environment(timestamp: str) -> dict[str, str]:
    return {
        "GIT_COMMITTER_DATE": timestamp,
        "GIT_COMMITTER_EMAIL": "tagger@example.test",
        "GIT_COMMITTER_NAME": "Tagger Person",
    }


def test_collects_annotated_alias_and_lightweight_tags_with_honest_slots(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "tags")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    repo.run(
        "tag",
        "-a",
        "v1.0.0",
        commit_id,
        "-m",
        "first release",
        environment=_tagger_environment("1704067200 +0545"),
    )
    tag_object_id = repo.run("rev-parse", "refs/tags/v1.0.0").decode("ascii").strip()
    repo.point_ref("refs/tags/v1-alias", tag_object_id)
    repo.point_ref("refs/tags/latest", commit_id)

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitTagCollector().collect(repositories)

    assert not result.diagnostics
    assert result.requested_repositories == 1
    assert result.successful_repositories == 1
    assert result.discovered_tags == 3
    assert result.annotated_tags == 2
    assert result.lightweight_tags == 1
    assert result.captured_tagger_timestamps == 2
    assert result.unavailable_tagger_timestamps == 1
    (accounting,) = result.repository_accounting
    assert accounting.repository_root == repo.path.resolve()
    assert accounting.repository_identity == repositories[0].identity
    assert accounting.discovered_tags == 3
    assert accounting.captured_tags == accounting.eligible_tags == 3
    assert accounting.record_errors == 0
    assert accounting.captured_tagger_timestamps == 2
    assert accounting.unavailable_tagger_timestamps == 1
    assert accounting.scope_matches == 2
    assert accounting.operational_errors == 0
    assert accounting.successful
    with pytest.raises(FrozenInstanceError):
        accounting.captured_tags = 2  # type: ignore[misc]
    by_ref = {item.ref.ref_name: item for item in result.tags}
    assert set(by_ref) == {"refs/tags/latest", "refs/tags/v1-alias", "refs/tags/v1.0.0"}
    assert not by_ref["refs/tags/latest"].annotated
    assert by_ref["refs/tags/latest"].tag_object_id is None
    assert by_ref["refs/tags/latest"].target_id == commit_id
    with pytest.raises(ValueError, match="no independent"):
        by_ref["refs/tags/latest"].to_observation()

    annotated = by_ref["refs/tags/v1.0.0"]
    alias = by_ref["refs/tags/v1-alias"]
    assert annotated.tag_object_id == alias.tag_object_id == tag_object_id
    assert annotated.target_id == alias.target_id == commit_id
    assert annotated.subject == "first release"
    assert annotated.tagger is not None
    assert annotated.tagger.raw_timestamp == "1704067200 +0545"
    assert annotated.to_origin().record_id != alias.to_origin().record_id

    origins = tuple(item.to_origin() for item in result.tags)
    observations = tuple(item.to_observation() for item in result.tags if item.tagger is not None)
    assert len(origins) == 3
    assert len(observations) == 2
    assert all(item.record_kind is RecordKind.TAG for item in origins)
    assert all(item.kind is TimestampKind.GIT_TAGGER for item in observations)
    assert observations[0].original_offset_minutes == 345
    assert observations[0].actor_name == "Tagger Person"


def test_annotated_tag_without_tagger_is_an_unavailable_slot_not_a_fabricated_date(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "no-tagger")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    raw_tag = (f"object {commit_id}\ntype commit\ntag no-tagger\n\nmessage\n").encode()
    tag_object = repo.run(
        "hash-object",
        "--literally",
        "-t",
        "tag",
        "-w",
        "--stdin",
        input_data=raw_tag,
    )
    repo.point_ref("refs/tags/no-tagger", tag_object.decode("ascii").strip())

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitTagCollector().collect(repositories)

    assert not result.diagnostics
    assert result.annotated_tags == 1
    assert result.captured_tagger_timestamps == 0
    assert result.unavailable_tagger_timestamps == 1
    assert len(result.tags) == 1
    assert result.tags[0].annotated
    assert result.tags[0].tagger is None
    assert result.tags[0].to_origin().target_id == commit_id
    assert not tuple(item.to_observation() for item in result.tags if item.tagger is not None)


def test_aliases_share_one_batched_tag_object_read(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "aliases")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    repo.run(
        "tag",
        "-a",
        "one",
        commit_id,
        "-m",
        "message",
        environment=_tagger_environment("1700000001 +0000"),
    )
    object_id = repo.run("rev-parse", "refs/tags/one").decode("ascii").strip()
    repo.point_ref("refs/tags/two", object_id)
    repositories = GitCollector().collect((repo.path,)).repositories

    class RecordingRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__(stream_output=False)
            self.cat_file_inputs: list[bytes] = []

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
                self.cat_file_inputs.append(input_data)
            return super().run(
                arguments,
                cwd=cwd,
                input_data=input_data,
                allowed_returncodes=allowed_returncodes,
            )

    runner = RecordingRunner()
    result = GitTagCollector(runner).collect(repositories)

    assert len(result.tags) == 2
    assert runner.cat_file_inputs == [object_id.encode() + b"\n"]


def test_tag_collector_emits_bounded_ref_batches_without_retaining_records(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "batched-tags")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    for index in range(5):
        repo.point_ref(f"refs/tags/tag-{index}", commit_id)
    repositories = GitCollector().collect((repo.path,)).repositories
    batches: list[tuple[str, ...]] = []

    result = GitTagCollector(ref_batch_size=2).collect(
        repositories,
        tag_consumer=lambda tags: batches.append(tuple(item.ref.ref_name for item in tags)),
        retain_tags=False,
    )

    assert result.discovered_tags == 5
    assert result.captured_tagger_timestamps == 0
    assert result.unavailable_tagger_timestamps == 5
    assert result.tags == ()
    assert result.records_retained is False
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert {ref for batch in batches for ref in batch} == {f"refs/tags/tag-{index}" for index in range(5)}

    byte_bounded_batches: list[tuple[CollectedGitTag, ...]] = []
    GitTagCollector(ref_batch_size=5, tag_batch_size=10, tag_batch_bytes=1).collect(
        repositories,
        tag_consumer=byte_bounded_batches.append,
        retain_tags=False,
    )
    assert [len(batch) for batch in byte_bounded_batches] == [1, 1, 1, 1, 1]

    with pytest.raises(ValueError, match="ref_batch_size"):
        GitTagCollector(ref_batch_size=0)
    with pytest.raises(ValueError, match="tag_batch_size"):
        GitTagCollector(tag_batch_size=0)
    with pytest.raises(ValueError, match="tag_batch_bytes"):
        GitTagCollector(tag_batch_bytes=0)

    def fail_consumer(_batch: tuple[CollectedGitTag, ...]) -> None:
        raise OSError("downstream storage failed")

    with pytest.raises(OSError, match="downstream storage failed"):
        GitTagCollector().collect(
            repositories,
            tag_consumer=fail_consumer,
            retain_tags=False,
        )


def test_linked_worktree_contexts_share_one_tag_traversal(tmp_path: Path) -> None:
    primary = GitRepo.create(tmp_path / "primary")
    commit_id = primary.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    primary.point_ref("refs/tags/latest", commit_id)
    linked_path = tmp_path / "linked"
    primary.run("worktree", "add", "-b", "linked", str(linked_path))
    repositories = GitCollector().collect((primary.path, linked_path)).repositories

    result = GitTagCollector().collect(repositories)

    assert result.requested_repositories == 1
    assert result.successful_repositories == 1
    assert result.discovered_tags == 1
    assert len(result.tags) == 1
    assert len(result.repository_accounting) == 1


def test_tag_repository_accounting_rejects_invalid_partitions(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)
    valid = GitTagRepositoryAccounting(
        repository=repository,
        discovered_tags=2,
        captured_tags=2,
        record_errors=0,
        annotated_tags=1,
        lightweight_tags=1,
        captured_tagger_timestamps=1,
        unavailable_tagger_timestamps=1,
        scope_matches=1,
        unavailable_objects=0,
        parse_errors=0,
        operational_errors=0,
        successful=True,
    )

    with pytest.raises(ValueError, match="non-negative"):
        replace(valid, parse_errors=-1)
    with pytest.raises(ValueError, match="record accounting"):
        replace(valid, record_errors=1)
    with pytest.raises(ValueError, match="discovery accounting"):
        replace(valid, annotated_tags=2)
    with pytest.raises(ValueError, match="timestamp accounting"):
        replace(valid, captured_tagger_timestamps=0)
    with pytest.raises(ValueError, match="scope matches exceed"):
        replace(valid, scope_matches=2)


def test_tag_parsers_reject_malformed_records_and_preserve_raw_signature() -> None:
    object_id = "a" * 40
    target_id = "b" * 40
    raw = (
        f"object {target_id}\n"
        "type commit\n"
        "tag release\n"
        "tagger Ada Example <ada@example.test> -1 -0230\n"
        "\n"
        "subject\nbody\n"
    ).encode()
    parsed = parse_tag_object(object_id, raw)
    assert parsed.target_id == target_id
    assert parsed.tagger is not None
    assert parsed.tagger.raw_timestamp == "-1 -0230"
    assert parsed.subject == "subject"

    refs = parse_tag_refs(f"refs/tags/v1\0{object_id}\0tag\0\n".encode())
    assert refs[0].ref_name == "refs/tags/v1"
    assert refs[0].annotated

    with pytest.raises(GitTagParseError) as malformed_ref:
        parse_tag_refs(b"refs/tags/v1\0not-an-oid\0tag\0\n")
    assert malformed_ref.value.code == "invalid_git_tag_ref"

    with pytest.raises(GitTagParseError) as malformed_object:
        parse_tag_object(object_id, b"object missing-boundary")
    assert malformed_object.value.code == "invalid_tag_object"


def test_tag_ref_parser_rejects_each_invalid_machine_field() -> None:
    object_id = b"a" * 40
    assert parse_tag_refs(b"") == ()

    invalid_payloads = (
        b"missing-fields\n",
        b"refs/heads/not-a-tag\0" + object_id + b"\0commit\0\n",
        b"refs/tags/\0" + object_id + b"\0commit\0\n",
        b"refs/tags/name\0not-an-oid\0commit\0\n",
        b"refs/tags/name\0" + object_id + b"\0unknown\0\n",
    )
    for payload in invalid_payloads:
        with pytest.raises(GitTagParseError) as error:
            parse_tag_refs(payload)
        assert error.value.code == "invalid_git_tag_ref"


def test_tag_object_parser_validates_headers_and_target_fields() -> None:
    object_id = "a" * 40
    target_id = "b" * 40

    with pytest.raises(GitTagParseError) as invalid_id:
        parse_tag_object("invalid", b"ignored")
    assert invalid_id.value.code == "invalid_tag_object_id"

    malformed_objects = (
        (b" continuation\n\nmessage", "invalid_tag_header"),
        (f"object {target_id}\ntype commit\ntag release\nbad\n\nmessage".encode(), "invalid_tag_header"),
        (
            f"object {target_id}\nobject {target_id}\ntype commit\ntag release\n\nmessage".encode(),
            "invalid_tag_header",
        ),
        (f"object {target_id}\ntype commit\n\nmessage".encode(), "invalid_tag_header"),
        (b"object invalid\ntype commit\ntag release\n\nmessage", "invalid_tag_header"),
        (f"object {target_id}\ntype invalid\ntag release\n\nmessage".encode(), "invalid_tag_header"),
        (
            f"object {target_id}\ntype commit\ntag release\ntagger Person <p@example.test> nope +0000\n\nmessage".encode(),
            "invalid_git_timestamp",
        ),
    )
    for payload, expected_code in malformed_objects:
        with pytest.raises(GitTagParseError) as error:
            parse_tag_object(object_id, payload)
        assert error.value.code == expected_code

    signed = (f"object {target_id}\ntype commit\ntag release\ngpgsig signature\n continuation\n\nmessage\n").encode()
    assert parse_tag_object(object_id, signed).tagger is None


@pytest.mark.parametrize(
    ("fault", "expected_code", "expected_parse_errors"),
    [
        ("discovery-command", "git_command_failed", 0),
        ("discovery-parse", "invalid_git_tag_ref", 1),
        ("object-command", "git_command_failed", 1),
        ("batch-envelope", "truncated_cat_file_batch", 1),
        ("object-unavailable", "git_tag_object_unavailable", 1),
        ("wrong-object-type", "git_object_not_tag", 1),
    ],
)
def test_tag_collector_structures_discovery_and_object_failures(
    tmp_path: Path,
    fault: str,
    expected_code: str,
    expected_parse_errors: int,
) -> None:
    object_id = "a" * 40
    repository = GitRepository(tmp_path, tmp_path / ".git", tmp_path / ".git", False)
    valid_refs = f"refs/tags/release\0{object_id}\0tag\0\n".encode()

    class FaultRunner(GitRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            cwd: Path,
            input_data: bytes | None = None,
            allowed_returncodes: Collection[int] = (0,),
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "for-each-ref":
                if fault == "discovery-command":
                    raise GitCommandError(
                        code="git_command_failed",
                        message="tag discovery failed",
                        command=tuple(arguments),
                        cwd=cwd,
                        stderr=b"discovery details",
                    )
                output = b"malformed\n" if fault == "discovery-parse" else valid_refs
                return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")
            if fault == "object-command":
                raise GitCommandError(
                    code="git_command_failed",
                    message="tag object read failed",
                    command=tuple(arguments),
                    cwd=cwd,
                )
            if fault == "batch-envelope":
                output = b"broken"
            elif fault == "object-unavailable":
                output = f"{object_id} missing\n".encode()
            else:
                output = f"{object_id} blob 0\n\n".encode()
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

    result = GitTagCollector(FaultRunner(stream_output=False)).collect((repository,))

    assert result.is_partial
    assert [item.code for item in result.diagnostics] == [expected_code]
    assert result.parse_errors == expected_parse_errors
    (accounting,) = result.repository_accounting
    assert accounting.discovered_tags == (0 if fault.startswith("discovery") else 1)
    assert accounting.captured_tags == 0
    assert accounting.record_errors == accounting.discovered_tags
    assert accounting.operational_errors == 1
    assert accounting.successful is (fault in {"object-unavailable", "wrong-object-type"})
    if fault == "discovery-command":
        assert "discovery details" in result.diagnostics[0].message
    if fault == "object-unavailable":
        assert result.unavailable_objects == 1


def test_tag_collector_handles_repository_without_tags(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "empty-tags")
    repositories = GitCollector().collect((repo.path,)).repositories

    result = GitTagCollector().collect(repositories)

    assert result.successful_repositories == 1
    assert result.discovered_tags == 0
    assert not result.tags
    assert not result.is_partial


def test_large_annotated_tag_message_is_streamed_and_keeps_complete_timestamp_coverage(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "large-tag")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    raw_tag = (
        f"object {commit_id}\ntype commit\ntag large\ntagger Tagger <tagger@example.test> 1700000001 +0000\n\n"
    ).encode() + b"s" * 1_100_000
    tag_object = repo.run("hash-object", "-t", "tag", "-w", "--stdin", input_data=raw_tag).decode().strip()
    repo.point_ref("refs/tags/large", tag_object)

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitTagCollector().collect(repositories)

    assert result.captured_tagger_timestamps == 1
    assert result.tags[0].tagger is not None
    assert len(result.tags[0].subject or "") == 1_048_577
    assert [item.code for item in result.diagnostics] == ["git_tag_subject_truncated"]
    assert not result.is_partial


def test_malformed_annotated_object_is_diagnostic_and_not_reclassified_lightweight(
    tmp_path: Path,
) -> None:
    repo = GitRepo.create(tmp_path / "malformed")
    commit_id = repo.commit(
        "one.txt",
        "one",
        "one",
        author_date="1700000000 +0000",
        committer_date="1700000000 +0000",
    )
    malformed = (
        f"object {commit_id}\ntype commit\ntag malformed\ntagger Person <person@example.test> nope +0000\n\nmessage\n"
    ).encode()
    tag_object = repo.run(
        "hash-object",
        "--literally",
        "-t",
        "tag",
        "-w",
        "--stdin",
        input_data=malformed,
    )
    repo.point_ref("refs/tags/malformed", tag_object.decode("ascii").strip())

    repositories = GitCollector().collect((repo.path,)).repositories
    result = GitTagCollector().collect(repositories)

    assert result.discovered_tags == 1
    assert result.annotated_tags == 1
    assert result.lightweight_tags == 0
    assert result.parse_errors == 1
    assert not result.tags
    assert result.diagnostics[0].code == "invalid_git_timestamp"
