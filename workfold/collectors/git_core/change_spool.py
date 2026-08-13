"""Bounded provisional storage for one streamed Git commit's file changes."""

from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Final, cast

from workfold.collectors.git_core.diff_tree import ParsedGitChange
from workfold.models import GitChangeKind

_RECORD_HEADER: Final[struct.Struct] = struct.Struct("!HBqII")
_NO_OLD_PATH: Final[int] = (1 << 32) - 1


class GitChangeSpoolError(RuntimeError):
    """A local failure while staging changes before commit completion."""


class GitChangeSpool:
    """Stage one commit's changes in bounded memory until its boundary is proven.

    Small commits remain in memory. Large commits transparently roll over to an
    automatically removed temporary file. Only source-preserving primitive
    fields are stored; decoded paths are reconstructed when the commit is
    released.
    """

    def __init__(self, *, memory_limit: int) -> None:
        if memory_limit < 1:
            raise ValueError("Git change spool memory limit must be positive")
        try:
            self._stream = cast(
                BinaryIO,
                tempfile.SpooledTemporaryFile(max_size=memory_limit, mode="w+b"),
            )
        except OSError as error:
            raise GitChangeSpoolError(f"could not create the temporary Git change spool: {error}") from error
        self._commit_id: str | None = None
        self._record_count = 0

    def __enter__(self) -> GitChangeSpool:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stream.close()

    def stage(self, change: ParsedGitChange) -> None:
        """Append one provisional change without publishing it to consumers."""

        if self._commit_id is None:
            self._commit_id = change.commit_id
        elif change.commit_id != self._commit_id:
            raise GitChangeSpoolError("Git change stream crossed a commit boundary without completion")

        status = change.raw_status.encode("ascii")
        kind = change.change_kind.value.encode("ascii")
        old_path = change.raw_old_path
        old_path_size = _NO_OLD_PATH if old_path is None else len(old_path)
        similarity = -1 if change.similarity is None else change.similarity
        try:
            header = _RECORD_HEADER.pack(
                len(status),
                len(kind),
                similarity,
                len(change.raw_path),
                old_path_size,
            )
            self._stream.write(header)
            self._stream.write(status)
            self._stream.write(kind)
            self._stream.write(change.raw_path)
            if old_path is not None:
                self._stream.write(old_path)
        except (OSError, struct.error) as error:
            raise GitChangeSpoolError(f"could not write the temporary Git change spool: {error}") from error
        self._record_count += 1

    def release(self, commit_id: str) -> Iterator[ParsedGitChange]:
        """Validate, yield, and reset one fully bounded commit.

        Validation is deliberately a complete first pass.  A truncated or
        corrupt spool must fail before a consumer observes any record from the
        affected commit.  The second pass reconstructs records lazily so a
        valid, exceptionally large commit remains bounded in memory.
        """

        if self._commit_id not in {None, commit_id}:
            raise GitChangeSpoolError("Git change completion does not match its staged commit")
        try:
            self._stream.flush()
            self._validate(commit_id)
            self._stream.seek(0)
            for _ in range(self._record_count):
                yield self._read_change(commit_id)
            self._stream.seek(0)
            self._stream.truncate(0)
        except OSError as error:
            raise GitChangeSpoolError(f"could not read the temporary Git change spool: {error}") from error
        self._commit_id = None
        self._record_count = 0

    def _validate(self, commit_id: str) -> None:
        """Read the complete staged representation before publication."""

        self._stream.seek(0)
        for _ in range(self._record_count):
            self._read_change(commit_id)
        if self._stream.read(1):
            raise GitChangeSpoolError("temporary Git change spool contains trailing data")

    def _read_change(self, commit_id: str) -> ParsedGitChange:
        header = _read_exact(self._stream, _RECORD_HEADER.size)
        if len(header) != _RECORD_HEADER.size:
            raise GitChangeSpoolError("temporary Git change spool ended inside a record header")
        try:
            status_size, kind_size, similarity, path_size, old_path_size = _RECORD_HEADER.unpack(header)
            status_raw = _read_required(self._stream, status_size, field="status")
            kind_raw = _read_required(self._stream, kind_size, field="change kind")
            raw_path = _read_required(self._stream, path_size, field="path")
            raw_old_path = (
                None if old_path_size == _NO_OLD_PATH else _read_required(self._stream, old_path_size, field="old path")
            )
            raw_status = status_raw.decode("ascii")
            change_kind = GitChangeKind(kind_raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError, struct.error) as error:
            raise GitChangeSpoolError(f"temporary Git change spool contains invalid metadata: {error}") from error
        return ParsedGitChange(
            commit_id=commit_id,
            raw_status=raw_status,
            change_kind=change_kind,
            path=Path(os.fsdecode(raw_path)),
            raw_path=raw_path,
            old_path=Path(os.fsdecode(raw_old_path)) if raw_old_path is not None else None,
            raw_old_path=raw_old_path,
            similarity=None if similarity < 0 else similarity,
        )


def _read_required(stream: BinaryIO, size: int, *, field: str) -> bytes:
    value = _read_exact(stream, size)
    if len(value) != size:
        raise GitChangeSpoolError(f"temporary Git change spool ended inside a {field}")
    return value


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = ["GitChangeSpool", "GitChangeSpoolError"]
