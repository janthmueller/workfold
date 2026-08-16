from __future__ import annotations

import pytest
from workfold.collection.filesystem.scan import ValidatedBatchPublisher


def test_publishes_at_the_exact_batch_boundary_after_validation() -> None:
    published: list[int] = []
    validated_states: list[tuple[int, ...]] = []
    publisher: ValidatedBatchPublisher[int] = ValidatedBatchPublisher(
        validator=lambda: validated_states.append(tuple(published)),
        consumer=published.append,
        batch_size=2,
    )

    publisher.stage(1)
    assert published == []
    assert validated_states == []

    publisher.stage(2)
    assert published == [1, 2]
    assert validated_states == [()]

    publisher.stage(3)
    publisher.flush()
    assert published == [1, 2, 3]
    assert validated_states == [(), (1, 2)]


def test_failed_validation_discards_the_unpublished_batch() -> None:
    published: list[int] = []
    fail_validation = True

    def validate() -> None:
        if fail_validation:
            raise RuntimeError("scope changed")

    publisher = ValidatedBatchPublisher(
        validator=validate,
        consumer=published.append,
        batch_size=1,
    )

    with pytest.raises(RuntimeError, match="scope changed"):
        publisher.stage(1)

    fail_validation = False
    publisher.flush()
    assert published == []


def test_empty_flush_does_not_revalidate_the_scope() -> None:
    validations: list[None] = []
    publisher: ValidatedBatchPublisher[int] = ValidatedBatchPublisher(
        validator=lambda: validations.append(None),
        consumer=lambda _item: None,
        batch_size=1,
    )

    publisher.flush()

    assert validations == []


def test_publication_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="publication batch size must be positive"):
        ValidatedBatchPublisher(validator=lambda: None, consumer=lambda _item: None, batch_size=0)
