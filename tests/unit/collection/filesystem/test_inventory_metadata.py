from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

import pytest
from workfold.collection.filesystem.inventory_metadata import (
    AnchoredInventoryMetadata,
    anchored_inventory_metadata_supported,
)
from workfold.collection.filesystem.scan import DirectorySafetyError, RootSnapshot

pytestmark = pytest.mark.skipif(
    not anchored_inventory_metadata_supported(),
    reason="component-safe descriptor-relative metadata is unavailable",
)


def test_reads_nested_entries_without_following_the_final_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    file = nested / "work.txt"
    file.write_text("work", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("external", encoding="utf-8")
    link = nested / "link"
    link.symlink_to(external)

    with AnchoredInventoryMetadata(RootSnapshot(root, os.lstat(root)), statx_reader=None) as metadata:
        file_snapshot = metadata.read(PurePosixPath("nested/work.txt"), display_path=file)
        link_snapshot = metadata.read(PurePosixPath("nested/link"), display_path=link)

    assert stat.S_ISREG(file_snapshot.st_mode)
    assert (file_snapshot.st_dev, file_snapshot.st_ino) == (os.lstat(file).st_dev, os.lstat(file).st_ino)
    assert stat.S_ISLNK(link_snapshot.st_mode)
    assert link_snapshot.st_ino == os.lstat(link).st_ino
    assert link_snapshot.st_ino != os.stat(external).st_ino


def test_rejects_an_intermediate_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("external", encoding="utf-8")
    (root / "escape").symlink_to(external, target_is_directory=True)

    with AnchoredInventoryMetadata(RootSnapshot(root, os.lstat(root)), statx_reader=None) as metadata:
        with pytest.raises(DirectorySafetyError, match="without following symbolic links"):
            metadata.read(PurePosixPath("escape/secret.txt"), display_path=root / "escape" / secret.name)


@pytest.mark.parametrize("relative", [PurePosixPath("../outside.txt"), PurePosixPath("/outside.txt")])
def test_rejects_a_path_outside_the_root(tmp_path: Path, relative: PurePosixPath) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with AnchoredInventoryMetadata(RootSnapshot(root, os.lstat(root)), statx_reader=None) as metadata:
        with pytest.raises(DirectorySafetyError, match="normalized descendant"):
            metadata.read(relative, display_path=tmp_path / "outside.txt")


def test_rejects_a_replaced_root_before_opening_the_session(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    original = RootSnapshot(root, os.lstat(root))
    root.rename(tmp_path / "original")
    root.mkdir()

    with pytest.raises(DirectorySafetyError, match="identity changed"):
        with AnchoredInventoryMetadata(original, statx_reader=None):
            raise AssertionError("an identity-mismatched root must not open")


def test_small_descriptor_cache_reopens_evicted_directories_safely(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    files: list[Path] = []
    for name in ("one", "two", "three"):
        directory = root / name
        directory.mkdir()
        file = directory / "work.txt"
        file.write_text(name, encoding="utf-8")
        files.append(file)

    with AnchoredInventoryMetadata(
        RootSnapshot(root, os.lstat(root)),
        statx_reader=None,
        cache_limit=1,
    ) as metadata:
        snapshots = [
            metadata.read(PurePosixPath(file.relative_to(root).as_posix()), display_path=file)
            for file in (*files, files[0])
        ]

    assert all(stat.S_ISREG(snapshot.st_mode) for snapshot in snapshots)


def test_cache_limit_must_be_positive(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="cache limit must be positive"):
        AnchoredInventoryMetadata(RootSnapshot(root, os.lstat(root)), statx_reader=None, cache_limit=0)
