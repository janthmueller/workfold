from __future__ import annotations

import ctypes
import os
import sys
from errno import ENOSYS, EPERM
from pathlib import Path

import pytest
from workfold.collection.filesystem import FilesystemCollector
from workfold.collection.filesystem.linux import (
    LinuxStatxBirthTime,
    LinuxStatxCallError,
    LinuxStatxReader,
    LinuxStatxSnapshot,
)
from workfold.collection.filesystem.metadata import FilesystemTimestampAdapter
from workfold.domain.coverage import CapabilityStatus
from workfold.domain.observations import EntryType, TimestampKind

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux statx is available only on Linux")


def file_timestamps(
    *kinds: TimestampKind,
) -> tuple[tuple[EntryType, tuple[TimestampKind, ...]], ...]:
    return ((EntryType.REGULAR_FILE, kinds),)


def _reader_or_skip() -> LinuxStatxReader:
    reader = LinuxStatxReader()
    if not reader.available:
        pytest.skip("the running libc does not expose statx")
    return reader


def _snapshot_from_stat(result: os.stat_result) -> LinuxStatxSnapshot:
    return LinuxStatxSnapshot(
        st_mode=result.st_mode,
        st_dev=result.st_dev,
        st_ino=result.st_ino,
        st_atime_ns=result.st_atime_ns,
        st_mtime_ns=result.st_mtime_ns,
        st_ctime_ns=result.st_ctime_ns,
        birth_time_utc_ns=result.st_mtime_ns,
    )


def test_native_statx_reads_birth_time_and_matches_lstat_identity(tmp_path: Path) -> None:
    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")
    snapshot = os.lstat(path)

    result = _reader_or_skip().read(path)

    if result.instant_utc_ns is None:
        pytest.skip("the test filesystem does not expose STATX_BTIME")
    assert result.instant_utc_ns > 0
    assert (result.device_id, result.inode) == (snapshot.st_dev, snapshot.st_ino)


def test_native_statx_snapshot_matches_lstat_metadata(tmp_path: Path) -> None:
    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")
    reader = _reader_or_skip()

    result = reader.read_snapshot(path)
    expected = os.lstat(path)

    assert (result.st_mode, result.st_dev, result.st_ino) == (
        expected.st_mode,
        expected.st_dev,
        expected.st_ino,
    )
    assert (result.st_atime_ns, result.st_mtime_ns, result.st_ctime_ns) == (
        expected.st_atime_ns,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    )
    assert result.birth_time_utc_ns == reader.read(path).instant_utc_ns


def test_native_statx_snapshot_reads_relative_to_an_open_directory(tmp_path: Path) -> None:
    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")
    reader = _reader_or_skip()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = reader.read_snapshot_at(descriptor, path.name, display_path=path)
    finally:
        os.close(descriptor)

    expected = os.lstat(path)
    assert (result.st_mode, result.st_dev, result.st_ino) == (
        expected.st_mode,
        expected.st_dev,
        expected.st_ino,
    )
    assert (result.st_atime_ns, result.st_mtime_ns, result.st_ctime_ns) == (
        expected.st_atime_ns,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    )


def test_collection_uses_descriptor_relative_statx_for_descendants(tmp_path: Path) -> None:
    class TrackingReader(LinuxStatxReader):
        def __init__(self) -> None:
            self.absolute_snapshots: list[Path] = []
            self.relative_snapshots: list[Path] = []
            self.birth_reads: list[Path] = []

        @property
        def available(self) -> bool:
            return True

        def read(self, path: Path) -> LinuxStatxBirthTime:
            self.birth_reads.append(path)
            raise LinuxStatxCallError(EPERM, "independent capability probe denied", path)

        def read_snapshot(self, path: Path) -> LinuxStatxSnapshot:
            self.absolute_snapshots.append(path)
            return _snapshot_from_stat(os.lstat(path))

        def read_snapshot_at(
            self,
            directory_fd: int,
            name: str | bytes,
            *,
            display_path: Path,
        ) -> LinuxStatxSnapshot:
            self.relative_snapshots.append(display_path)
            return _snapshot_from_stat(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))

    root = tmp_path / "root"
    root.mkdir()
    child = root / "work.txt"
    child.write_text("work", encoding="utf-8")
    reader = TrackingReader()
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    result = FilesystemCollector(timestamp_adapter=adapter).collect(
        (root,),
        entry_timestamps=file_timestamps(TimestampKind.FS_CREATED, TimestampKind.FS_MODIFIED),
        respect_gitignore=False,
        include_ignored=True,
    )

    assert reader.absolute_snapshots == [root]
    assert reader.relative_snapshots == [child]
    assert reader.birth_reads == []
    creation_capability = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_CREATED)
    assert creation_capability.status is CapabilityStatus.SUPPORTED
    assert len(result.observations) == 2
    by_kind = {item.kind: item for item in result.observations}
    assert by_kind[TimestampKind.FS_CREATED].raw_timestamp.startswith("statx.stx_btime_ns=")
    assert by_kind[TimestampKind.FS_MODIFIED].raw_timestamp.startswith("st_mtime_ns=")


def test_native_statx_does_not_follow_the_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    result = _reader_or_skip().read(link)

    link_snapshot = os.lstat(link)
    target_snapshot = os.stat(target)
    assert (result.device_id, result.inode) == (link_snapshot.st_dev, link_snapshot.st_ino)
    assert result.inode != target_snapshot.st_ino


def test_native_statx_birth_time_flows_through_collection(tmp_path: Path) -> None:
    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")

    result = FilesystemCollector().collect(
        (path,),
        entry_timestamps=file_timestamps(TimestampKind.FS_CREATED),
        respect_gitignore=False,
        include_ignored=True,
    )

    capability = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_CREATED)
    if capability.status is CapabilityStatus.UNSUPPORTED:
        pytest.skip(capability.note or "Linux statx is unsupported")
    timestamp_coverage = result.accounting.timestamps[0]
    if timestamp_coverage.unavailable:
        pytest.skip("the test filesystem does not expose STATX_BTIME")
    assert timestamp_coverage.captured == 1
    assert timestamp_coverage.errors == 0
    assert len(result.observations) == 1
    assert result.observations[0].raw_timestamp.startswith("statx.stx_btime_ns=")


def test_statx_snapshot_failure_falls_back_without_losing_portable_timestamps(tmp_path: Path) -> None:
    class DeniedSnapshotReader(LinuxStatxReader):
        def __init__(self) -> None:
            self.birth_reads = 0

        @property
        def available(self) -> bool:
            return True

        def read_snapshot(self, path: Path) -> LinuxStatxSnapshot:
            raise LinuxStatxCallError(EPERM, "statx denied", path)

        def read(self, path: Path) -> LinuxStatxBirthTime:
            self.birth_reads += 1
            result = os.lstat(path)
            return LinuxStatxBirthTime(result.st_mtime_ns, result.st_dev, result.st_ino)

    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")
    reader = DeniedSnapshotReader()
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    result = FilesystemCollector(timestamp_adapter=adapter).collect(
        (path,),
        entry_timestamps=file_timestamps(TimestampKind.FS_CREATED, TimestampKind.FS_MODIFIED),
        respect_gitignore=False,
        include_ignored=True,
    )

    coverage = {item.key.timestamp_kind: item for item in result.accounting.timestamps}
    assert result.successful_roots == (path,)
    assert coverage[TimestampKind.FS_CREATED].errors == 1
    assert coverage[TimestampKind.FS_MODIFIED].captured == 1
    assert [item.kind for item in result.observations] == [TimestampKind.FS_MODIFIED]
    assert any(item.code == "filesystem_timestamp_error" for item in result.diagnostics)
    creation_capability = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_CREATED)
    assert creation_capability.status is CapabilityStatus.UNAVAILABLE
    assert reader.birth_reads == 0


def test_failed_combined_statx_is_not_followed_by_separate_birthtime_reads(tmp_path: Path) -> None:
    class DeniedReader(LinuxStatxReader):
        def __init__(self) -> None:
            self.snapshot_reads = 0
            self.birth_reads = 0

        @property
        def available(self) -> bool:
            return True

        def read_snapshot(self, path: Path) -> LinuxStatxSnapshot:
            self.snapshot_reads += 1
            raise LinuxStatxCallError(EPERM, "statx denied", path)

        def read_snapshot_at(
            self,
            directory_fd: int,
            name: str | bytes,
            *,
            display_path: Path,
        ) -> LinuxStatxSnapshot:
            del directory_fd, name
            self.snapshot_reads += 1
            raise LinuxStatxCallError(EPERM, "statx denied", display_path)

        def read(self, path: Path) -> LinuxStatxBirthTime:
            self.birth_reads += 1
            raise LinuxStatxCallError(EPERM, "statx denied", path)

    root = tmp_path / "root"
    root.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (root / name).write_text(name, encoding="utf-8")
    reader = DeniedReader()
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    result = FilesystemCollector(timestamp_adapter=adapter).collect(
        (root,),
        entry_timestamps=file_timestamps(TimestampKind.FS_CREATED, TimestampKind.FS_MODIFIED),
        respect_gitignore=False,
        include_ignored=True,
        retain_entries=False,
        retain_observations=False,
    )

    coverage = {item.key.timestamp_kind: item for item in result.accounting.timestamps}
    assert reader.snapshot_reads == 4
    assert reader.birth_reads == 0
    assert coverage[TimestampKind.FS_CREATED].errors == 3
    assert coverage[TimestampKind.FS_MODIFIED].captured == 3


def test_statx_enosys_disables_future_calls_and_reports_unsupported_birth_time(tmp_path: Path) -> None:
    calls = 0

    def missing_statx(*_arguments: object) -> int:
        nonlocal calls
        calls += 1
        ctypes.set_errno(ENOSYS)
        return -1

    root = tmp_path / "root"
    root.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (root / name).write_text(name, encoding="utf-8")
    reader = LinuxStatxReader()
    reader._function = missing_statx  # type: ignore[reportPrivateUsage]
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    result = FilesystemCollector(timestamp_adapter=adapter).collect(
        (root,),
        entry_timestamps=file_timestamps(TimestampKind.FS_CREATED, TimestampKind.FS_MODIFIED),
        respect_gitignore=False,
        include_ignored=True,
        retain_entries=False,
        retain_observations=False,
    )

    coverage = {item.key.timestamp_kind: item for item in result.accounting.timestamps}
    creation_capability = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_CREATED)
    assert calls == 1
    assert not reader.available
    assert creation_capability.status is CapabilityStatus.UNSUPPORTED
    assert coverage[TimestampKind.FS_CREATED].unsupported == 3
    assert coverage[TimestampKind.FS_MODIFIED].captured == 3
    assert not result.diagnostics
