from pathlib import Path

import pytest
from wuf.domain.observations import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from wuf.folding.pipeline import ObservationBatch


def _observation(kind: TimestampKind, *, record_id: str = "commit-1") -> TimestampObservation:
    origin = RecordOrigin(record_id, Source.GIT, RecordKind.COMMIT, Path("/repo"), commit_id="a" * 40)
    return TimestampObservation.create(
        origin,
        kind,
        1_000_000_000,
        "1 +0000",
        original_offset_minutes=0,
        actor_name="Ada",
        actor_email="ada@example.test",
    )


def test_observation_batch_enforces_one_nonempty_record_without_duplicate_slots() -> None:
    author = _observation(TimestampKind.GIT_AUTHOR)
    committer = _observation(TimestampKind.GIT_COMMITTER)
    assert ObservationBatch.create((author, committer)).observations == (author, committer)

    with pytest.raises(ValueError, match="cannot be empty"):
        ObservationBatch.create(())
    with pytest.raises(ValueError, match="one source record"):
        ObservationBatch.create((author, _observation(TimestampKind.GIT_AUTHOR, record_id="other")))
    with pytest.raises(ValueError, match="duplicate"):
        ObservationBatch.create((author, author))
