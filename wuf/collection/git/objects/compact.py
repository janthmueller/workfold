"""Bounded provenance extraction from byte-counted Git objects."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from typing import BinaryIO, Final

from wuf.collection.git.objects.cat_file import (
    SizedObjectReader,
    read_batch_header,
    read_batch_terminator,
)
from wuf.collection.git.objects.commit_parser import parse_commit_object
from wuf.collection.git.objects.models import (
    CommitBatchResult,
    CompactBatchObject,
    CompactBatchResult,
    GitObjectParseError,
    InvalidBatchCommit,
    InvalidBatchObject,
    UnavailableBatchObject,
    UnexpectedBatchObject,
)

_MAX_OBJECT_HEADER_LINE_BYTES: Final[int] = 1_048_576
_MAX_RETAINED_OBJECT_HEADER_BYTES: Final[int] = 4_194_304
_MAX_OBJECT_SUBJECT_BYTES: Final[int] = 1_048_576


def read_cat_file_batch_commit(
    stream: BinaryIO,
    *,
    expected_object_id: str | None = None,
    expect_metadata: bool = False,
) -> tuple[CommitBatchResult, bytes | None]:
    """Retain bounded commit provenance while draining the full raw object."""

    response, metadata = read_batch_header(
        stream,
        expected_object_id=expected_object_id,
        expect_metadata=expect_metadata,
    )
    if isinstance(response, UnavailableBatchObject):
        return response, metadata

    body = SizedObjectReader(stream, response.size, object_id=response.object_id)
    if response.object_type != "commit":
        body.drain()
        read_batch_terminator(stream, object_id=response.object_id)
        return UnexpectedBatchObject(response.object_id, response.object_type), metadata

    compact_data: bytes | None = None
    subject_truncated = False
    semantic_error: GitObjectParseError | None = None
    try:
        compact_data, subject_truncated = _read_compact_object_data(
            body,
            object_id=response.object_id,
            retained_names=frozenset({b"tree", b"parent", b"author", b"committer", b"encoding"}),
            object_label="commit",
        )
    except GitObjectParseError as error:
        semantic_error = error
    body.drain()
    read_batch_terminator(stream, object_id=response.object_id)

    if semantic_error is not None:
        return _invalid_batch_commit(semantic_error, object_id=response.object_id), metadata
    if compact_data is None:
        raise AssertionError("commit metadata parser returned no result")
    try:
        parsed = parse_commit_object(response.object_id, compact_data)
    except GitObjectParseError as error:
        return _invalid_batch_commit(error, object_id=response.object_id), metadata
    if subject_truncated:
        parsed = replace(
            parsed,
            subject=f"{parsed.subject}…",
            subject_truncated=True,
        )
    return parsed, metadata


def read_cat_file_batch_compact_object(
    stream: BinaryIO,
    *,
    expected_object_type: str,
    retained_header_names: Collection[bytes],
    object_label: str,
    expected_object_id: str | None = None,
) -> CompactBatchResult:
    """Retain bounded headers and a bounded subject from one raw object."""

    response, metadata = read_batch_header(
        stream,
        expected_object_id=expected_object_id,
        expect_metadata=False,
    )
    if metadata is not None:
        raise AssertionError("ordinary cat-file batch output cannot contain request metadata")
    if isinstance(response, UnavailableBatchObject):
        return response

    body = SizedObjectReader(stream, response.size, object_id=response.object_id)
    if response.object_type != expected_object_type:
        body.drain()
        read_batch_terminator(stream, object_id=response.object_id)
        return UnexpectedBatchObject(response.object_id, response.object_type)

    compact_data: bytes | None = None
    subject_truncated = False
    semantic_error: GitObjectParseError | None = None
    try:
        compact_data, subject_truncated = _read_compact_object_data(
            body,
            object_id=response.object_id,
            retained_names=frozenset(retained_header_names),
            object_label=object_label,
        )
    except GitObjectParseError as error:
        semantic_error = error
    body.drain()
    read_batch_terminator(stream, object_id=response.object_id)

    if semantic_error is not None:
        return InvalidBatchObject(
            object_id=semantic_error.object_id or response.object_id,
            code=semantic_error.code,
            message=str(semantic_error),
        )
    if compact_data is None:
        raise AssertionError("compact object metadata parser returned no result")
    return CompactBatchObject(
        object_id=response.object_id,
        object_type=response.object_type,
        data=compact_data,
        subject_truncated=subject_truncated,
    )


def _read_compact_object_data(
    body: SizedObjectReader,
    *,
    object_id: str,
    retained_names: frozenset[bytes],
    object_label: str,
) -> tuple[bytes, bool]:
    retained_headers: list[bytes] = []
    retained_size = 0
    current_name: bytes | None = None
    found_boundary = False

    while body.remaining:
        line, oversized, terminated = _read_limited_object_line(body, _MAX_OBJECT_HEADER_LINE_BYTES)
        if line == b"" and terminated:
            found_boundary = True
            break
        if line.startswith(b" "):
            if current_name is None:
                raise GitObjectParseError(
                    f"invalid_{object_label}_header",
                    f"orphaned continuation line in {object_label} header",
                    object_id=object_id,
                )
        else:
            name, separator, _value = line.partition(b" ")
            if not separator or not name:
                raise GitObjectParseError(
                    f"invalid_{object_label}_header",
                    f"malformed {object_label} header line",
                    object_id=object_id,
                )
            current_name = name
            if name in retained_names:
                if oversized:
                    raise GitObjectParseError(
                        f"oversized_{object_label}_metadata",
                        f"{object_label} {name.decode('ascii')} header exceeds the supported metadata limit",
                        object_id=object_id,
                    )
                retained_size += len(line) + 1
                if retained_size > _MAX_RETAINED_OBJECT_HEADER_BYTES:
                    raise GitObjectParseError(
                        f"oversized_{object_label}_metadata",
                        f"{object_label} headers exceed the supported retained-metadata limit",
                        object_id=object_id,
                    )
                retained_headers.append(line)
        if not terminated:
            break

    if not found_boundary:
        raise GitObjectParseError(
            f"invalid_{object_label}_object",
            f"{object_label} object has no header/message boundary",
            object_id=object_id,
        )

    raw_subject = b""
    subject_truncated = False
    if body.remaining:
        raw_subject, subject_truncated, _terminated = _read_limited_object_line(
            body,
            _MAX_OBJECT_SUBJECT_BYTES,
        )
    return b"\n".join(retained_headers) + b"\n\n" + raw_subject, subject_truncated


def _read_limited_object_line(
    body: SizedObjectReader,
    content_limit: int,
) -> tuple[bytes, bool, bool]:
    """Read a bounded line, discarding any excess without retaining it."""

    raw = body.readline(content_limit + 2)
    terminated = raw.endswith(b"\n")
    content = raw[:-1] if terminated else raw
    oversized = len(content) > content_limit
    if not terminated and body.remaining:
        terminated = body.discard_line_tail()
        oversized = True
    return content[:content_limit], oversized, terminated


def _invalid_batch_commit(error: GitObjectParseError, *, object_id: str) -> InvalidBatchCommit:
    return InvalidBatchCommit(
        object_id=error.object_id or object_id,
        code=error.code,
        message=str(error),
    )


__all__ = ["read_cat_file_batch_commit", "read_cat_file_batch_compact_object"]
