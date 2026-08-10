from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workfold.models import (
    ActivityMarker,
    ClassifiedMarker,
    EntryType,
    GitChangeKind,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
    coalesce_observations,
)
from workfold.provenance import (
    activity_marker_id,
    canonical_bytes,
    canonical_id,
    filesystem_entry_id,
    git_commit_id,
    git_file_change_id,
    git_reflog_id,
    git_tag_id,
    lexical_absolute,
    observation_id,
    repository_id,
)


def _git_origin(record_id: str = "commit-1") -> RecordOrigin:
    return RecordOrigin(
        record_id,
        Source.GIT,
        RecordKind.COMMIT,
        Path("/repo"),
        commit_id="a" * 40,
        description="subject",
    )


def _observation(
    kind: TimestampKind,
    *,
    origin: RecordOrigin | None = None,
    instant: int = 1_000_000_000,
) -> TimestampObservation:
    return TimestampObservation.create(
        origin or _git_origin(),
        kind,
        instant,
        "1 +0000",
        original_offset_minutes=0,
        actor_name="Ada",
        actor_email="ada@example.test",
    )


def test_provenance_is_deterministic_domain_separated_and_length_delimited(tmp_path: Path) -> None:
    assert canonical_id("record", "ab", "c") == canonical_id("record", "ab", "c")
    assert canonical_id("record", "ab", "c") != canonical_id("record", "a", "bc")
    assert canonical_id("record", b"a") != canonical_id("record", "a")
    assert canonical_id("record", 1) != canonical_id("record", "1")
    assert canonical_id("record", None) != canonical_id("record", "")
    assert canonical_bytes("x", Path("a")) != canonical_bytes("x", "a")
    assert lexical_absolute("child", base=tmp_path) == tmp_path / "child"


def test_specialized_provenance_includes_every_identity_dimension(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    assert repository_id(repository) != repository_id(tmp_path / "other")
    assert git_commit_id(repository, "a" * 40) != git_commit_id(repository, "b" * 40)
    assert git_file_change_id(repository, "a", "parent", "R100", "old", "new") != git_file_change_id(
        repository, "a", "parent", "R100", "old", "other"
    )
    assert git_tag_id(repository, "refs/tags/v1", "tag", "target") != git_tag_id(
        repository, "refs/tags/v2", "tag", "target"
    )
    assert git_reflog_id(repository, "HEAD", "a", "b", "HEAD@{0}", "1 +0000", "Ada", "move", 0) != git_reflog_id(
        repository, "HEAD", "a", "b", "HEAD@{0}", "1 +0000", "Ada", "move", 1
    )
    assert filesystem_entry_id(repository, "linked", "symlink") != filesystem_entry_id(
        repository, "linked", "regular_file"
    )


def test_provenance_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="unsupported"):
        canonical_id("bad", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unsupported"):
        canonical_id("bad", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        git_reflog_id("/repo", "HEAD", "a", "b", "selector", "raw", "actor", "message", -1)
    with pytest.raises(ValueError, match="at least one"):
        activity_marker_id(())
    with pytest.raises(ValueError, match="duplicate"):
        activity_marker_id(("same", "same"))


def test_origin_and_observation_factories_preserve_roles() -> None:
    origin = _git_origin()
    observation = _observation(TimestampKind.GIT_AUTHOR, origin=origin)

    assert origin.provenance_id == "commit-1"
    assert observation.observation_id == observation_id(origin.record_id, TimestampKind.GIT_AUTHOR.value)
    assert observation.timestamp_kind.source is Source.GIT
    assert observation.epoch_ns == 1_000_000_000
    assert observation.raw_timestamp == "1 +0000"


def test_origin_rejects_inconsistent_semantics() -> None:
    with pytest.raises(ValueError, match="record_id"):
        RecordOrigin("", Source.GIT, RecordKind.COMMIT, Path("/repo"))
    with pytest.raises(ValueError, match="filesystem source"):
        RecordOrigin("x", Source.GIT, RecordKind.FILESYSTEM_ENTRY, Path("/repo"))
    with pytest.raises(ValueError, match="Git record"):
        RecordOrigin("x", Source.FILESYSTEM, RecordKind.COMMIT, Path("/repo"))
    with pytest.raises(ValueError, match="commit_id"):
        RecordOrigin("x", Source.GIT, RecordKind.COMMIT, Path("/repo"))
    with pytest.raises(ValueError, match="file-change origins"):
        RecordOrigin("x", Source.GIT, RecordKind.GIT_FILE_CHANGE, Path("/repo"))
    with pytest.raises(ValueError, match="tag origins"):
        RecordOrigin("x", Source.GIT, RecordKind.TAG, Path("/repo"))
    with pytest.raises(ValueError, match="reflog origins"):
        RecordOrigin("x", Source.GIT, RecordKind.REFLOG, Path("/repo"))
    with pytest.raises(ValueError, match="require a path"):
        RecordOrigin("x", Source.FILESYSTEM, RecordKind.FILESYSTEM_ENTRY, Path("/root"))
    with pytest.raises(ValueError, match="change_kind"):
        RecordOrigin(
            "x",
            Source.GIT,
            RecordKind.COMMIT,
            Path("/repo"),
            commit_id="a" * 40,
            change_kind=GitChangeKind.MODIFIED,
        )
    with pytest.raises(ValueError, match="entry_type"):
        RecordOrigin(
            "x",
            Source.GIT,
            RecordKind.COMMIT,
            Path("/repo"),
            commit_id="a" * 40,
            entry_type=EntryType.DIRECTORY,
        )


def test_observation_validation() -> None:
    filesystem_origin = RecordOrigin(
        "file",
        Source.FILESYSTEM,
        RecordKind.FILESYSTEM_ENTRY,
        Path("/root"),
        path=Path("file"),
        entry_type=EntryType.REGULAR_FILE,
    )
    with pytest.raises(ValueError, match="does not belong"):
        TimestampObservation.create(filesystem_origin, TimestampKind.GIT_AUTHOR, 1, "1")
    with pytest.raises(ValueError, match="recorded UTC offset"):
        TimestampObservation.create(_git_origin(), TimestampKind.GIT_AUTHOR, 1, "1")
    with pytest.raises(ValueError, match="recorded identity"):
        TimestampObservation.create(
            _git_origin(),
            TimestampKind.GIT_AUTHOR,
            1,
            "1 +0000",
            original_offset_minutes=0,
        )
    with pytest.raises(ValueError, match="cannot carry Git identity"):
        TimestampObservation.create(
            filesystem_origin,
            TimestampKind.FS_MODIFIED,
            1,
            "1",
            actor_name="not applicable",
        )
    with pytest.raises(ValueError, match="raw_timestamp"):
        TimestampObservation.create(filesystem_origin, TimestampKind.FS_MODIFIED, 1, "")
    assert TimestampKind.FS_MODIFIED.source is Source.FILESYSTEM
    with pytest.raises(ValueError, match="observation_id"):
        TimestampObservation("", _git_origin(), TimestampKind.GIT_AUTHOR, 1, "1")
    with pytest.raises(ValueError, match="offset"):
        TimestampObservation.create(_git_origin(), TimestampKind.GIT_AUTHOR, 1, "1 +2400", original_offset_minutes=1440)


def test_only_identical_author_and_committer_observations_coalesce() -> None:
    author = _observation(TimestampKind.GIT_AUTHOR)
    committer = _observation(TimestampKind.GIT_COMMITTER)
    coalesced = coalesce_observations((committer, author))

    assert len(coalesced) == 1
    assert coalesced[0].timestamp_roles == (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER)
    assert coalesced[0].origin == author.origin
    assert coalesced[0].sources == frozenset((Source.GIT,))

    later_committer = _observation(TimestampKind.GIT_COMMITTER, instant=2_000_000_000)
    other_origin = _observation(TimestampKind.GIT_COMMITTER, origin=_git_origin("commit-2"))
    assert len(coalesce_observations((author, later_committer))) == 2
    assert len(coalesce_observations((author, other_origin))) == 2


def test_duplicate_roles_are_not_silently_coalesced() -> None:
    origin = _git_origin()
    first = _observation(TimestampKind.GIT_AUTHOR, origin=origin)
    second = TimestampObservation(
        "second-author",
        origin,
        TimestampKind.GIT_AUTHOR,
        first.instant_utc_ns,
        "duplicate",
        0,
        "Ada",
        "ada@example.test",
    )
    committer = _observation(TimestampKind.GIT_COMMITTER, origin=origin)
    assert len(coalesce_observations((first, second, committer))) == 3


def test_activity_marker_rejects_forbidden_constituents() -> None:
    author = _observation(TimestampKind.GIT_AUTHOR)
    committer = _observation(TimestampKind.GIT_COMMITTER)
    with pytest.raises(ValueError, match="at least one"):
        ActivityMarker.create(())
    with pytest.raises(ValueError, match="marker_id"):
        ActivityMarker("", author.instant_utc_ns, (author,))
    with pytest.raises(ValueError, match="at least one"):
        ActivityMarker("marker", author.instant_utc_ns, ())
    with pytest.raises(ValueError, match="duplicate"):
        ActivityMarker("marker", author.instant_utc_ns, (author, author))
    with pytest.raises(ValueError, match="marker instant"):
        ActivityMarker("marker", 2, (author,))
    with pytest.raises(ValueError, match="same record"):
        ActivityMarker(
            "marker", 1_000_000_000, (author, _observation(TimestampKind.GIT_COMMITTER, origin=_git_origin("two")))
        )
    with pytest.raises(ValueError, match="author/committer"):
        ActivityMarker(
            "marker",
            1_000_000_000,
            (
                author,
                TimestampObservation(
                    "other",
                    author.origin,
                    TimestampKind.GIT_AUTHOR,
                    1_000_000_000,
                    "raw",
                    0,
                    "Ada",
                    "ada@example.test",
                ),
            ),
        )
    with pytest.raises(ValueError, match="at most two"):
        ActivityMarker("marker", 1_000_000_000, (author, committer, author))


def test_classified_marker_derives_folded_wall_clock_fields_without_binning() -> None:
    marker = ActivityMarker.create((_observation(TimestampKind.GIT_AUTHOR),))
    local = datetime(2026, 8, 3, 8, 14, tzinfo=ZoneInfo("Europe/Berlin"))
    classified = ClassifiedMarker(marker, local, True)

    assert classified.weekday.abbreviation == "Mo"
    assert not classified.weekday.is_weekend
    assert classified.minute_of_day == 8 * 60 + 14
    assert classified.time_of_day_ns == (8 * 60 + 14) * 60 * 1_000_000_000
    assert classified.within_schedule
    assert not classified.weekend


def test_classified_marker_restores_exact_submicrosecond_time_of_day() -> None:
    origin = _git_origin()
    local = datetime(2026, 8, 3, 8, 14, 15, 123456, tzinfo=ZoneInfo("UTC"))
    base_ns = 1_775_463_255_123_456_000
    observation = TimestampObservation.create(
        origin,
        TimestampKind.GIT_AUTHOR,
        base_ns + 789,
        str(base_ns + 789),
        original_offset_minutes=0,
        actor_name="Ada",
        actor_email="ada@example.test",
    )
    classified = ClassifiedMarker(ActivityMarker.create((observation,)), local, True)

    assert classified.time_of_day_ns == ((8 * 60 + 14) * 60 + 15) * 1_000_000_000 + 123_456_789


def test_classified_marker_requires_aware_datetime() -> None:
    marker = ActivityMarker.create((_observation(TimestampKind.GIT_AUTHOR),))
    with pytest.raises(ValueError, match="timezone-aware"):
        ClassifiedMarker(marker, datetime(2026, 8, 3, 8), True)
