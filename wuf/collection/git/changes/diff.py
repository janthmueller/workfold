"""Bounded, NUL-safe parsing for streamed ``git diff-tree`` output."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Final

from wuf.domain.observations import GitChangeKind

_STATUS_RE: Final[re.Pattern[bytes]] = re.compile(rb"([A-Z])([0-9]{1,3})?\Z")
_STREAM_READ_BYTES: Final[int] = 65_536
_MAX_TOKEN_BYTES: Final[int] = 16 * 1_024 * 1_024


class GitChangeParseError(ValueError):
    """A structured failure to parse ``diff-tree -z`` output."""

    def __init__(self, code: str, message: str, *, commit_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.commit_id = commit_id


@dataclass(frozen=True, slots=True)
class ParsedGitChange:
    """One exact name-status record emitted for a selected commit."""

    commit_id: str
    raw_status: str
    change_kind: GitChangeKind
    path: Path
    raw_path: bytes
    old_path: Path | None = None
    raw_old_path: bytes | None = None
    similarity: int | None = None


@dataclass(frozen=True, slots=True)
class DiffCommitComplete:
    """Boundary proving that one requested commit was parsed completely."""

    commit_id: str
    at_stream_end: bool = False


DiffTreeRecord = ParsedGitChange | DiffCommitComplete


class _NulTokenReader:
    """Read bounded NUL-delimited tokens without retaining the whole stream."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._eof = False

    def read(
        self,
        *,
        commit_id: str | None = None,
        allow_unterminated: bool = False,
    ) -> bytes | None:
        while True:
            terminator = self._buffer.find(0)
            if terminator >= 0:
                token = bytes(self._buffer[:terminator])
                del self._buffer[: terminator + 1]
                return token
            if self._eof:
                if self._buffer:
                    if allow_unterminated:
                        token = bytes(self._buffer)
                        self._buffer.clear()
                        return token
                    raise GitChangeParseError(
                        "truncated_git_change",
                        "diff-tree output ended inside a NUL-delimited record",
                        commit_id=commit_id,
                    )
                return None
            chunk = self._stream.read(_STREAM_READ_BYTES)
            if not chunk:
                self._eof = True
                continue
            self._buffer.extend(chunk)
            if len(self._buffer) > _MAX_TOKEN_BYTES:
                raise GitChangeParseError(
                    "oversized_git_change_token",
                    "diff-tree returned a path or record larger than the supported limit",
                    commit_id=commit_id,
                )


def iter_diff_tree_name_status(
    stream: BinaryIO,
    expected_commit_ids: tuple[str, ...],
) -> Iterator[DiffTreeRecord]:
    """Yield changes and verified commit boundaries from a diff-tree stream."""

    if not expected_commit_ids:
        if stream.read(1):
            raise GitChangeParseError("unexpected_git_change_output", "diff-tree returned unrequested data")
        return
    tokens = _NulTokenReader(stream)

    pending_header: bytes | None = None
    for index, expected_id in enumerate(expected_commit_ids):
        expected_raw = expected_id.encode("ascii")
        header = (
            pending_header
            if pending_header is not None
            else tokens.read(commit_id=expected_id, allow_unterminated=True)
        )
        pending_header = None
        if header != expected_raw:
            raise GitChangeParseError(
                "unexpected_git_change_commit",
                "diff-tree response order does not match its request",
                commit_id=expected_id,
            )
        if tokens.read(commit_id=expected_id) != b"":
            raise GitChangeParseError(
                "invalid_git_change_boundary",
                "diff-tree commit header has no NUL boundary",
                commit_id=expected_id,
            )

        next_id = expected_commit_ids[index + 1].encode("ascii") if index + 1 < len(expected_commit_ids) else None
        first_status = True
        while True:
            token = tokens.read(commit_id=expected_id)
            if token is None:
                if next_id is not None:
                    raise GitChangeParseError(
                        "unexpected_git_change_commit",
                        "diff-tree output ended before the next requested commit",
                        commit_id=expected_commit_ids[index + 1],
                    )
                yield DiffCommitComplete(expected_id, at_stream_end=True)
                break
            if next_id is not None and token == next_id:
                pending_header = token
                yield DiffCommitComplete(expected_id)
                break
            if first_status and token.startswith(b"\n"):
                token = token[1:]
            first_status = False
            match = _STATUS_RE.fullmatch(token)
            if match is None:
                raise GitChangeParseError(
                    "invalid_git_change_status",
                    "diff-tree returned an invalid name-status token",
                    commit_id=expected_id,
                )
            status_letter, score_raw = match.groups()
            similarity = int(score_raw) if score_raw is not None else None
            if similarity is not None and similarity > 100:
                raise GitChangeParseError(
                    "invalid_git_change_status",
                    "diff-tree returned an out-of-range similarity score",
                    commit_id=expected_id,
                )
            path_count = 2 if status_letter in {b"R", b"C"} else 1
            raw_paths: list[bytes] = []
            for _ in range(path_count):
                raw_path = tokens.read(commit_id=expected_id)
                if raw_path is None:
                    raise GitChangeParseError(
                        "truncated_git_change",
                        "diff-tree output ended inside a path record",
                        commit_id=expected_id,
                    )
                raw_paths.append(raw_path)
            raw_old_path = raw_paths[0] if path_count == 2 else None
            raw_path = raw_paths[-1]
            yield ParsedGitChange(
                commit_id=expected_id,
                raw_status=token.decode("ascii"),
                change_kind=_change_kind(status_letter),
                path=_decode_path(raw_path, commit_id=expected_id),
                raw_path=raw_path,
                old_path=(_decode_path(raw_old_path, commit_id=expected_id) if raw_old_path is not None else None),
                raw_old_path=raw_old_path,
                similarity=similarity,
            )

    if pending_header is not None or tokens.read() is not None:
        raise GitChangeParseError(
            "unexpected_git_change_output",
            "diff-tree returned trailing unrequested data",
        )


def parse_diff_tree_name_status(
    payload: bytes,
    expected_commit_ids: tuple[str, ...],
) -> tuple[ParsedGitChange, ...]:
    """Parse an in-memory response through the same streaming state machine."""

    return tuple(
        item
        for item in iter_diff_tree_name_status(BytesIO(payload), expected_commit_ids)
        if isinstance(item, ParsedGitChange)
    )


def _change_kind(status: bytes) -> GitChangeKind:
    return {
        b"A": GitChangeKind.ADDED,
        b"M": GitChangeKind.MODIFIED,
        b"D": GitChangeKind.DELETED,
        b"R": GitChangeKind.RENAMED,
    }.get(status, GitChangeKind.OTHER)


def decode_git_tree_path(raw_path: bytes) -> Path:
    """Decode Git's opaque path bytes with a reversible, host-independent codec."""

    return Path(raw_path.decode("utf-8", errors="surrogateescape"))


def _decode_path(raw_path: bytes, *, commit_id: str) -> Path:
    if not raw_path:
        raise GitChangeParseError(
            "invalid_git_change_path",
            "git diff-tree returned an empty path",
            commit_id=commit_id,
        )
    return decode_git_tree_path(raw_path)


__all__ = [
    "DiffCommitComplete",
    "DiffTreeRecord",
    "GitChangeParseError",
    "ParsedGitChange",
    "decode_git_tree_path",
    "iter_diff_tree_name_status",
    "parse_diff_tree_name_status",
]
