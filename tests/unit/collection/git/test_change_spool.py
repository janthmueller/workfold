from __future__ import annotations

import tempfile
from pathlib import Path
from typing import NoReturn

import pytest
from wuf.collection.git.changes.diff import ParsedGitChange
from wuf.collection.git.changes.spool import GitChangeSpool, GitChangeSpoolError
from wuf.domain.observations import GitChangeKind


def _change(
    commit_id: str,
    *,
    raw_path: bytes,
    raw_old_path: bytes | None = None,
    raw_status: str = "A",
    kind: GitChangeKind = GitChangeKind.ADDED,
    similarity: int | None = None,
) -> ParsedGitChange:
    return ParsedGitChange(
        commit_id=commit_id,
        raw_status=raw_status,
        change_kind=kind,
        path=Path(raw_path.decode(errors="surrogateescape")),
        raw_path=raw_path,
        old_path=Path(raw_old_path.decode(errors="surrogateescape")) if raw_old_path is not None else None,
        raw_old_path=raw_old_path,
        similarity=similarity,
    )


def test_change_spool_round_trips_source_bytes_and_resets_between_commits() -> None:
    first_id = "a" * 40
    second_id = "b" * 40
    first = _change(first_id, raw_path=b"invalid-\xff-name", raw_status="M", kind=GitChangeKind.MODIFIED)
    renamed = _change(
        first_id,
        raw_path=b"new-name",
        raw_old_path=b"old-name",
        raw_status="R075",
        kind=GitChangeKind.RENAMED,
        similarity=75,
    )
    second = _change(second_id, raw_path=b"next")

    # One byte forces the implementation through its disk-backed rollover path.
    with GitChangeSpool(memory_limit=1) as spool:
        spool.stage(first)
        spool.stage(renamed)
        assert tuple(spool.release(first_id)) == (first, renamed)

        spool.stage(second)
        assert tuple(spool.release(second_id)) == (second,)


def test_change_spool_rejects_crossed_commit_boundaries() -> None:
    with GitChangeSpool(memory_limit=1_024) as spool:
        spool.stage(_change("a" * 40, raw_path=b"one"))
        with pytest.raises(GitChangeSpoolError, match="crossed a commit boundary"):
            spool.stage(_change("b" * 40, raw_path=b"two"))
        with pytest.raises(GitChangeSpoolError, match="completion does not match"):
            tuple(spool.release("b" * 40))


def test_change_spool_structures_temporary_file_creation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("temporary storage unavailable")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", fail)

    with pytest.raises(GitChangeSpoolError, match="temporary storage unavailable"):
        GitChangeSpool(memory_limit=1)


def test_change_spool_validates_the_complete_commit_before_publication() -> None:
    commit_id = "a" * 40
    first = _change(commit_id, raw_path=b"first")
    second = _change(commit_id, raw_path=b"second")
    published: list[ParsedGitChange] = []

    with GitChangeSpool(memory_limit=1_024) as spool:
        spool.stage(first)
        spool.stage(second)
        # Simulate a local spool becoming truncated after staging.  The first
        # record is intact, but it still must not escape a failed commit.
        spool._stream.seek(-1, 2)  # pyright: ignore[reportPrivateUsage]
        spool._stream.truncate()  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(GitChangeSpoolError, match="ended inside a path"):
            for change in spool.release(commit_id):
                published.append(change)

    assert published == []
