"""Bounded raw-object hydration for preselected Git commits."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, Final

from wuf.collection.git.batches import batched
from wuf.collection.git.objects.cat_file import parse_cat_file_batch
from wuf.collection.git.objects.commit_parser import parse_commit_object
from wuf.collection.git.objects.compact import read_cat_file_batch_commit
from wuf.collection.git.objects.models import (
    BatchObject,
    CommitBatchResult,
    GitObjectParseError,
    GitSignatureRole,
    InvalidBatchCommit,
    UnavailableBatchObject,
    UnexpectedBatchObject,
)
from wuf.collection.git.repository import GitRepository
from wuf.collection.git.runner import GitRunner

CommitRoleSelection = tuple[GitSignatureRole, ...]
_STREAM_BATCH_FORMAT: Final[str] = "--batch=%(objectname) %(objecttype) %(objectsize) %(rest)"
_ROLE_CODES: Final[dict[CommitRoleSelection, bytes]] = {
    ("author",): b"a",
    ("committer",): b"c",
    ("author", "committer"): b"ac",
    ("committer", "author"): b"ca",
}
_CODE_ROLES: Final[dict[bytes, CommitRoleSelection]] = {code: roles for roles, code in _ROLE_CODES.items()}


class GitObjectReadError(RuntimeError):
    """A structured local-I/O failure around raw commit hydration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def spool_commit_candidate(
    spool: BinaryIO,
    object_id: str,
    roles: CommitRoleSelection,
) -> None:
    """Append one ASCII object request and its selected timestamp roles."""

    try:
        role_code = _ROLE_CODES[roles]
    except KeyError as error:
        raise ValueError("commit candidate contains unsupported timestamp roles") from error
    try:
        spool.write(object_id.encode("ascii") + b" " + role_code + b"\n")
    except OSError as error:
        raise GitObjectReadError(
            "git_candidate_spool_error",
            f"temporary Git candidate inventory could not be written: {error}",
        ) from error


def iter_spooled_commit_objects(
    spool: BinaryIO,
    *,
    candidate_count: int,
    runner: GitRunner,
    repository: GitRepository,
    fallback_batch_size: int,
) -> Iterator[tuple[CommitRoleSelection | None, CommitBatchResult]]:
    """Yield parsed candidate commits through one production Git process.

    Custom process-runner adapters retain a compatibility batch path.
    Production passes the temporary request file directly to one byte-counted
    ``cat-file`` process and retains only bounded commit metadata. ``%(rest)``
    round-trips the compact role code without keeping an object-ID map in
    memory; Git omits that metadata for unavailable objects, which callers
    account without assigning an identity-scope outcome.
    """

    if candidate_count < 0:
        raise ValueError("candidate count must be non-negative")
    if fallback_batch_size < 1:
        raise ValueError("fallback batch size must be positive")
    try:
        spool.flush()
        spool.seek(0)
    except OSError as error:
        raise GitObjectReadError(
            "git_candidate_spool_error",
            f"temporary Git candidate inventory could not be read: {error}",
        ) from error
    if runner.streams_subprocess_output:
        yield from _iter_streamed_objects(
            spool,
            candidate_count=candidate_count,
            runner=runner,
            repository=repository,
        )
        return
    yield from _iter_buffered_objects(
        spool,
        candidate_count=candidate_count,
        runner=runner,
        repository=repository,
        batch_size=fallback_batch_size,
    )


def _iter_streamed_objects(
    spool: BinaryIO,
    *,
    candidate_count: int,
    runner: GitRunner,
    repository: GitRepository,
) -> Iterator[tuple[CommitRoleSelection | None, CommitBatchResult]]:
    command = ("cat-file", _STREAM_BATCH_FORMAT)
    try:
        with runner.open_stdout(command, cwd=repository.root, input_stream=spool) as stdout:
            for _ in range(candidate_count):
                result, metadata = read_cat_file_batch_commit(stdout, expect_metadata=True)
                object_id = _result_object_id(result)
                roles = None if metadata is None else _parse_role_code(metadata, object_id=object_id)
                yield roles, result
            if stdout.read(1):
                raise GitObjectParseError(
                    "unexpected_cat_file_output",
                    "cat-file batch returned trailing unrequested output",
                )
    except OSError as error:
        raise GitObjectReadError(
            "git_object_stream_error",
            f"Git object stream could not be read: {error}",
        ) from error


def _iter_buffered_objects(
    spool: BinaryIO,
    *,
    candidate_count: int,
    runner: GitRunner,
    repository: GitRepository,
    batch_size: int,
) -> Iterator[tuple[CommitRoleSelection | None, CommitBatchResult]]:
    request_count = 0
    try:
        for line_batch in batched(spool, batch_size):
            requests = tuple(_parse_candidate_line(line) for line in line_batch)
            request_count += len(requests)
            object_ids = tuple(object_id for object_id, _ in requests)
            roles_by_id = {object_id: roles for object_id, roles in requests}
            output = runner.run(
                ("cat-file", "--batch"),
                cwd=repository.root,
                input_data=b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids),
            ).stdout
            parsed = parse_cat_file_batch(output, object_ids)
            for result in (*parsed.objects, *parsed.unavailable):
                commit_result = _to_commit_result(result)
                yield roles_by_id[_result_object_id(commit_result)], commit_result
    except OSError as error:
        raise GitObjectReadError(
            "git_candidate_spool_error",
            f"temporary Git candidate inventory could not be read: {error}",
        ) from error
    if request_count != candidate_count:
        raise GitObjectParseError(
            "invalid_commit_candidate",
            "spooled commit candidate count does not reconcile",
        )


def _parse_candidate_line(line: bytes) -> tuple[str, CommitRoleSelection]:
    object_id_raw, separator, role_code = line.rstrip(b"\n").partition(b" ")
    if not separator:
        raise GitObjectParseError("invalid_commit_candidate", "malformed spooled commit candidate")
    try:
        object_id = object_id_raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise GitObjectParseError("invalid_commit_candidate", "non-ASCII spooled commit object ID") from error
    return object_id, _parse_role_code(role_code, object_id=object_id)


def _parse_role_code(value: bytes, *, object_id: str) -> CommitRoleSelection:
    try:
        return _CODE_ROLES[value]
    except KeyError as error:
        raise GitObjectParseError(
            "invalid_commit_candidate",
            "cat-file returned invalid commit candidate metadata",
            object_id=object_id,
        ) from error


def _to_commit_result(result: BatchObject | UnavailableBatchObject) -> CommitBatchResult:
    if isinstance(result, UnavailableBatchObject):
        return result
    if result.object_type != "commit":
        return UnexpectedBatchObject(result.object_id, result.object_type)
    try:
        return parse_commit_object(result.object_id, result.data)
    except GitObjectParseError as error:
        return InvalidBatchCommit(
            object_id=error.object_id or result.object_id,
            code=error.code,
            message=str(error),
        )


def _result_object_id(result: CommitBatchResult) -> str:
    return result.requested_id if isinstance(result, UnavailableBatchObject) else result.object_id


__all__ = [
    "CommitRoleSelection",
    "GitObjectReadError",
    "iter_spooled_commit_objects",
    "spool_commit_candidate",
]
