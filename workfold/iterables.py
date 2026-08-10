"""Small typed iterable helpers shared by bounded-memory collectors."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import TypeVar

_Item = TypeVar("_Item")


def batched(iterable: Iterable[_Item], size: int) -> Iterator[tuple[_Item, ...]]:
    """Yield fixed-size tuples without materializing the complete iterable."""

    if size < 1:
        raise ValueError("batch size must be positive")
    iterator = iter(iterable)
    while batch := tuple(islice(iterator, size)):
        yield batch


__all__ = ["batched"]
