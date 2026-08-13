from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from workfold.collection.filesystem import FilesystemCollector
from workfold.collection.filesystem.linux import LinuxStatxReader
from workfold.domain.coverage import CapabilityStatus
from workfold.domain.observations import TimestampKind

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux statx is available only on Linux")


def _reader_or_skip() -> LinuxStatxReader:
    reader = LinuxStatxReader()
    if not reader.available:
        pytest.skip("the running libc does not expose statx")
    return reader


def test_native_statx_reads_birth_time_and_matches_lstat_identity(tmp_path: Path) -> None:
    path = tmp_path / "work.txt"
    path.write_text("work", encoding="utf-8")
    snapshot = os.lstat(path)

    result = _reader_or_skip().read(path)

    if result.instant_utc_ns is None:
        pytest.skip("the test filesystem does not expose STATX_BTIME")
    assert result.instant_utc_ns > 0
    assert (result.device_id, result.inode) == (snapshot.st_dev, snapshot.st_ino)


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
        timestamp_kinds=(TimestampKind.FS_CREATED,),
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
