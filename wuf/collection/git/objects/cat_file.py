"""Byte-counted envelope handling for ``git cat-file --batch``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO, Final

from wuf.collection.git.objects.models import (
    BatchObject,
    BatchObjectResult,
    BatchParseResult,
    GitObjectParseError,
    UnavailableBatchObject,
)
from wuf.collection.git.objects.signatures import decode_losslessly

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_CAT_FILE_HEADER_BYTES: Final[int] = 512
_OBJECT_DRAIN_CHUNK_BYTES: Final[int] = 65_536


@dataclass(frozen=True, slots=True)
class BatchObjectHeader:
    """Validated header for one byte-counted batch response."""

    object_id: str
    object_type: str
    size: int


def parse_cat_file_batch(payload: bytes, expected_object_ids: tuple[str, ...]) -> BatchParseResult:
    """Parse buffered ``git cat-file --batch`` output through the exact protocol."""

    objects: list[BatchObject] = []
    unavailable: list[UnavailableBatchObject] = []
    stream = BytesIO(payload)
    for expected_id in expected_object_ids:
        result, metadata = read_cat_file_batch_record(stream, expected_object_id=expected_id)
        if metadata is not None:
            raise AssertionError("ordinary cat-file batch output cannot contain request metadata")
        if isinstance(result, BatchObject):
            objects.append(result)
        else:
            unavailable.append(result)
    if stream.read(1):
        raise GitObjectParseError(
            "unexpected_cat_file_output",
            "cat-file batch returned trailing unrequested output",
        )
    return BatchParseResult(objects=tuple(objects), unavailable=tuple(unavailable))


def read_cat_file_batch_record(
    stream: BinaryIO,
    *,
    expected_object_id: str | None = None,
    expect_metadata: bool = False,
) -> tuple[BatchObjectResult, bytes | None]:
    """Read one complete byte-counted batch response."""

    response, metadata = read_batch_header(
        stream,
        expected_object_id=expected_object_id,
        expect_metadata=expect_metadata,
    )
    if isinstance(response, UnavailableBatchObject):
        return response, metadata
    data = read_exact(stream, response.size)
    if len(data) != response.size:
        raise GitObjectParseError(
            "truncated_cat_file_batch",
            "cat-file batch output ended inside an object",
            object_id=response.object_id,
        )
    read_batch_terminator(stream, object_id=response.object_id)
    return BatchObject(response.object_id, response.object_type, data), metadata


def read_batch_header(
    stream: BinaryIO,
    *,
    expected_object_id: str | None,
    expect_metadata: bool,
) -> tuple[BatchObjectHeader | UnavailableBatchObject, bytes | None]:
    """Read and validate one batch response header without consuming its body."""

    header_with_newline = stream.readline(_MAX_CAT_FILE_HEADER_BYTES + 1)
    if not header_with_newline or not header_with_newline.endswith(b"\n"):
        raise GitObjectParseError(
            "truncated_cat_file_batch",
            "cat-file batch output ended before an object header",
            object_id=expected_object_id,
        )
    if len(header_with_newline) > _MAX_CAT_FILE_HEADER_BYTES:
        raise GitObjectParseError(
            "invalid_cat_file_header",
            "cat-file returned an oversized object header",
            object_id=expected_object_id,
        )
    fields = header_with_newline[:-1].split(b" ", 3 if expect_metadata else -1)
    if len(fields) == 2:
        requested_raw, reason_raw = fields
        requested_id = _validated_batch_object_id(requested_raw, expected_object_id)
        return UnavailableBatchObject(requested_id, decode_losslessly(reason_raw)), None

    expected_fields = 4 if expect_metadata else 3
    if len(fields) != expected_fields:
        raise GitObjectParseError(
            "invalid_cat_file_header",
            "malformed cat-file batch object header",
            object_id=expected_object_id,
        )
    returned_raw, object_type_raw, size_raw = fields[:3]
    object_id = _validated_batch_object_id(returned_raw, expected_object_id)
    try:
        size = int(size_raw)
    except ValueError as error:
        raise GitObjectParseError(
            "invalid_cat_file_header",
            "cat-file returned a non-integer object size",
            object_id=object_id,
        ) from error
    if size < 0:
        raise GitObjectParseError(
            "invalid_cat_file_header",
            "cat-file returned a negative object size",
            object_id=object_id,
        )
    metadata = fields[3] if expect_metadata else None
    return BatchObjectHeader(object_id, decode_losslessly(object_type_raw), size), metadata


class SizedObjectReader:
    """Read exactly one byte-counted object body without crossing its boundary."""

    __slots__ = ("_object_id", "_remaining", "_stream")

    def __init__(self, stream: BinaryIO, size: int, *, object_id: str) -> None:
        self._stream = stream
        self._remaining = size
        self._object_id = object_id

    @property
    def remaining(self) -> int:
        return self._remaining

    def readline(self, limit: int) -> bytes:
        if limit < 1:
            raise ValueError("object line limit must be positive")
        chunks: list[bytes] = []
        budget = min(limit, self._remaining)
        while budget:
            chunk = self._stream.readline(budget)
            if not chunk:
                self._raise_truncated()
            chunks.append(chunk)
            consumed = len(chunk)
            self._remaining -= consumed
            budget -= consumed
            if chunk.endswith(b"\n"):
                break
        return b"".join(chunks)

    def discard_line_tail(self) -> bool:
        """Discard through the next newline and report whether one existed."""

        while self._remaining:
            chunk = self.readline(min(_OBJECT_DRAIN_CHUNK_BYTES, self._remaining))
            if chunk.endswith(b"\n"):
                return True
        return False

    def drain(self) -> None:
        while self._remaining:
            requested = min(_OBJECT_DRAIN_CHUNK_BYTES, self._remaining)
            chunk = read_exact(self._stream, requested)
            if len(chunk) != requested:
                self._raise_truncated()
            self._remaining -= requested

    def _raise_truncated(self) -> None:
        raise GitObjectParseError(
            "truncated_cat_file_batch",
            "cat-file batch output ended inside an object",
            object_id=self._object_id,
        )


def read_batch_terminator(stream: BinaryIO, *, object_id: str) -> None:
    """Consume the newline separating one byte-counted object from the next."""

    if stream.read(1) != b"\n":
        raise GitObjectParseError(
            "invalid_cat_file_terminator",
            "cat-file object was not followed by its protocol newline",
            object_id=object_id,
        )


def read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read up to exactly ``size`` bytes, tolerating short buffered reads."""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validated_batch_object_id(value: bytes, expected_object_id: str | None) -> str:
    if not _OID_RE.fullmatch(value):
        raise GitObjectParseError(
            "unexpected_cat_file_object",
            "cat-file returned an invalid object ID",
            object_id=expected_object_id,
        )
    object_id = value.decode("ascii")
    if expected_object_id is not None and object_id != expected_object_id:
        raise GitObjectParseError(
            "unexpected_cat_file_object",
            "cat-file batch response order does not match its request",
            object_id=expected_object_id,
        )
    return object_id


__all__ = [
    "BatchObjectHeader",
    "SizedObjectReader",
    "parse_cat_file_batch",
    "read_batch_header",
    "read_batch_terminator",
    "read_cat_file_batch_record",
    "read_exact",
]
