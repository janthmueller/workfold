"""Internal filesystem collection types and injectable I/O boundaries."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from workfold.collection.filesystem.linux import LinuxStatxFallbackSnapshot, LinuxStatxReader
from workfold.domain.observations import EntryType, RecordOrigin, TimestampObservation


class StatSnapshot(Protocol):
    """No-follow metadata fields required by filesystem collection."""

    @property
    def st_mode(self) -> int: ...

    @property
    def st_dev(self) -> int: ...

    @property
    def st_ino(self) -> int: ...

    @property
    def st_atime_ns(self) -> int: ...

    @property
    def st_mtime_ns(self) -> int: ...

    @property
    def st_ctime_ns(self) -> int: ...


class DirectoryEntry(Protocol):
    """Minimal directory-entry surface consumed by native traversal."""

    @property
    def name(self) -> str: ...

    def stat(self, *, follow_symlinks: bool = True) -> StatSnapshot: ...


@dataclass(frozen=True, slots=True)
class RootSnapshot:
    path: Path
    snapshot: StatSnapshot


@dataclass(frozen=True, slots=True)
class PendingEntry:
    root: Path
    path: Path
    snapshot: StatSnapshot
    origin: RecordOrigin | None
    entry_type: EntryType | None

    @property
    def is_directory(self) -> bool:
        return self.entry_type is EntryType.DIRECTORY


class DirectorySafetyError(OSError):
    """A queued directory no longer satisfies no-follow traversal safety."""


LstatReader = Callable[[Path], StatSnapshot]
ScandirContext = AbstractContextManager[Iterator[DirectoryEntry]]
ScandirReader = Callable[[Path, StatSnapshot], ScandirContext]
FilesystemObservationConsumer = Callable[[tuple[TimestampObservation, ...]], None]


def lstat(path: Path) -> StatSnapshot:
    """Read one portable no-follow metadata snapshot."""

    return os.lstat(path)


def statx_fallback_snapshot(
    snapshot: StatSnapshot,
    error: OSError,
) -> LinuxStatxFallbackSnapshot:
    """Retain a failed combined syscall alongside portable metadata."""

    return LinuxStatxFallbackSnapshot(
        st_mode=snapshot.st_mode,
        st_dev=snapshot.st_dev,
        st_ino=snapshot.st_ino,
        st_atime_ns=snapshot.st_atime_ns,
        st_mtime_ns=snapshot.st_mtime_ns,
        st_ctime_ns=snapshot.st_ctime_ns,
        statx_error_number=error.errno,
        statx_error_message=str(error),
    )


def lstat_with_birthtime(path: Path, *, reader: LinuxStatxReader) -> StatSnapshot:
    """Prefer one combined ``statx`` snapshot without making it mandatory."""

    if reader.available:
        try:
            return reader.read_snapshot(path)
        except OSError as error:
            return statx_fallback_snapshot(lstat(path), error)
    return lstat(path)
