"""Internal filesystem collection types and injectable I/O boundaries."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from workfold.domain.observations import EntryType, RecordOrigin, TimestampObservation


@dataclass(frozen=True, slots=True)
class RootSnapshot:
    path: Path
    snapshot: os.stat_result


@dataclass(frozen=True, slots=True)
class PendingEntry:
    root: Path
    path: Path
    snapshot: os.stat_result
    origin: RecordOrigin
    entry_type: EntryType | None

    @property
    def is_directory(self) -> bool:
        return self.entry_type is EntryType.DIRECTORY


class DirectorySafetyError(OSError):
    """A queued directory no longer satisfies no-follow traversal safety."""


LstatReader = Callable[[Path], os.stat_result]
ScandirContext = AbstractContextManager[Iterator[os.DirEntry[str]]]
ScandirReader = Callable[[Path], ScandirContext]
FilesystemObservationConsumer = Callable[[tuple[TimestampObservation, ...]], None]


def lstat(path: Path) -> os.stat_result:
    return os.lstat(path)
