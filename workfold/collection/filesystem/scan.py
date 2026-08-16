"""Internal filesystem collection types and injectable I/O boundaries."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Generic, Protocol, TypeVar

from workfold.collection.filesystem.linux import LinuxStatxFallbackSnapshot, LinuxStatxReader
from workfold.domain.observations import EntryType, RecordOrigin, TimestampObservation

DIRECTORY_PUBLICATION_BATCH_SIZE: Final[int] = 4_096
_PublicationItem = TypeVar("_PublicationItem")


class ValidatedBatchPublisher(Generic[_PublicationItem]):
    """Publish bounded item batches only after validating their shared scope."""

    def __init__(
        self,
        *,
        validator: Callable[[], None],
        consumer: Callable[[_PublicationItem], None],
        batch_size: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError("publication batch size must be positive")
        self._validator = validator
        self._consumer = consumer
        self._batch_size = batch_size
        self._pending: list[_PublicationItem] = []

    def stage(self, item: _PublicationItem) -> None:
        """Retain one item and publish when the bounded batch is full."""

        self._pending.append(item)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Validate the shared scope, then publish the current batch."""

        if not self._pending:
            return
        pending = self._pending
        self._pending = []
        self._validator()
        for item in pending:
            self._consumer(item)


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


def revalidate_directory_snapshot(
    path: Path,
    expected_snapshot: StatSnapshot,
    lstat_reader: LstatReader,
) -> None:
    """Require *path* to remain the same no-follow directory snapshot."""

    try:
        actual = lstat_reader(path)
    except OSError as error:
        raise DirectorySafetyError(
            error.errno or errno.EIO,
            "queued directory could not be revalidated before filesystem discovery",
            os.fspath(path),
        ) from error
    validate_directory_snapshot_identity(path, actual, expected_snapshot)


def validate_directory_snapshot_identity(path: Path, actual: StatSnapshot, expected: StatSnapshot) -> None:
    """Require two snapshots to identify the same no-follow directory."""

    actual_identity = directory_identity(actual)
    expected_identity = directory_identity(expected)
    if (
        stat.S_ISDIR(actual.st_mode)
        and stat.S_ISDIR(expected.st_mode)
        and (actual_identity is None or expected_identity is None or actual_identity == expected_identity)
    ):
        return
    raise DirectorySafetyError(
        getattr(errno, "ESTALE", errno.EIO),
        "queued directory identity changed before or during traversal",
        os.fspath(path),
    )


def directory_identity(snapshot: StatSnapshot) -> tuple[int, int] | None:
    """Return a comparable identity when the metadata source exposes one."""

    identity = (snapshot.st_dev, snapshot.st_ino)
    return None if identity == (0, 0) else identity


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
