from __future__ import annotations

from pathlib import Path

from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.scope import ObservationScope
from workfold.time_ranges import InstantRange, InstantRangeUnion


def _git_observation(
    kind: TimestampKind,
    instant_utc_ns: int,
    *,
    name: str,
    email: str,
) -> TimestampObservation:
    origin = RecordOrigin(
        record_id="commit:one",
        source=Source.GIT,
        record_kind=RecordKind.COMMIT,
        repository_or_root=Path("/repo"),
        commit_id="a" * 40,
    )
    return TimestampObservation.create(
        origin,
        kind,
        instant_utc_ns,
        f"{instant_utc_ns} +0000",
        original_offset_minutes=0,
        actor_name=name,
        actor_email=email,
    )


def _filesystem_observation(instant_utc_ns: int) -> TimestampObservation:
    path = Path("/root/file.txt")
    origin = RecordOrigin(
        record_id="filesystem:file",
        source=Source.FILESYSTEM,
        record_kind=RecordKind.FILESYSTEM_ENTRY,
        repository_or_root=Path("/root"),
        path=path,
    )
    return TimestampObservation.create(
        origin,
        TimestampKind.FS_MODIFIED,
        instant_utc_ns,
        str(instant_utc_ns),
    )


def test_scope_uses_half_open_time_ranges() -> None:
    scope = ObservationScope(InstantRangeUnion((InstantRange(100, 200),)))

    assert scope.includes(_filesystem_observation(100))
    assert scope.includes(_filesystem_observation(199))
    assert not scope.includes(_filesystem_observation(99))
    assert not scope.includes(_filesystem_observation(200))


def test_git_identity_scope_is_case_insensitive_literal_or() -> None:
    scope = ObservationScope(
        InstantRangeUnion((InstantRange(None, None),)),
        ("ADA@EXAMPLE", "Release Bot"),
    )

    assert scope.includes(
        _git_observation(
            TimestampKind.GIT_AUTHOR,
            100,
            name="Ada Person",
            email="ada@example.test",
        )
    )
    assert scope.includes(
        _git_observation(
            TimestampKind.GIT_COMMITTER,
            100,
            name="Release Bot Account",
            email="bot@example.test",
        )
    )
    assert not scope.includes(
        _git_observation(
            TimestampKind.GIT_AUTHOR,
            100,
            name="Other Person",
            email="other@example.test",
        )
    )


def test_identity_scope_never_filters_filesystem_observations() -> None:
    scope = ObservationScope(
        InstantRangeUnion((InstantRange(None, None),)),
        ("does-not-match",),
    )

    assert scope.includes(_filesystem_observation(100))
    assert scope.is_restrictive_for(Source.GIT)
    assert not scope.is_restrictive_for(Source.FILESYSTEM)


def test_scope_selects_author_and_committer_roles_independently() -> None:
    author = _git_observation(
        TimestampKind.GIT_AUTHOR,
        150,
        name="Selected Author",
        email="author@example.test",
    )
    committer = _git_observation(
        TimestampKind.GIT_COMMITTER,
        250,
        name="Selected Author",
        email="author@example.test",
    )
    scope = ObservationScope(
        InstantRangeUnion((InstantRange(100, 200),)),
        ("author@example",),
    )

    assert scope.select((committer, author)) == (author,)
