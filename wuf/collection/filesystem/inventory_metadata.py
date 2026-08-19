"""Component-safe metadata access for Git filesystem inventories."""

from __future__ import annotations

import errno
import os
from collections import OrderedDict
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from types import TracebackType

from wuf.collection.filesystem.linux import LinuxStatxReader
from wuf.collection.filesystem.scan import (
    DirectorySafetyError,
    RootSnapshot,
    StatSnapshot,
    statx_fallback_snapshot,
    validate_directory_snapshot_identity,
)

_DIRECTORY_CACHE_LIMIT = 128


def anchored_inventory_metadata_supported() -> bool:
    """Return whether this OS can resolve every component relative to a handle."""

    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
    )


class AnchoredInventoryMetadata(AbstractContextManager["AnchoredInventoryMetadata"]):
    """Read inventory paths beneath one immutable root directory handle.

    Parent components are opened one at a time with no-follow semantics. A
    small LRU of directory descriptors preserves Git-inventory performance
    without allowing an intermediate symlink to redirect a later metadata
    lookup outside the selected root. Callers that publish incrementally must
    finish each bounded parent lease before making its snapshots visible.
    """

    def __init__(
        self,
        root_snapshot: RootSnapshot,
        *,
        statx_reader: LinuxStatxReader | None,
        cache_limit: int = _DIRECTORY_CACHE_LIMIT,
    ) -> None:
        if not anchored_inventory_metadata_supported():
            raise RuntimeError("component-safe descriptor-relative metadata is unavailable")
        if cache_limit < 1:
            raise ValueError("inventory directory cache limit must be positive")
        self._root_snapshot = root_snapshot
        self._statx_reader = statx_reader
        self._cache_limit = cache_limit
        self._root_descriptor: int | None = None
        self._directories: OrderedDict[tuple[str, ...], int] = OrderedDict()
        self._active_parent: tuple[tuple[str, ...], int] | None = None

    def __enter__(self) -> AnchoredInventoryMetadata:
        root = self._root_snapshot.path
        flags = _directory_open_flags()
        try:
            descriptor = os.open(root, flags)
        except OSError as error:
            message = (
                "queued directory became a non-directory or symlink before inventory collection"
                if error.errno in {errno.ELOOP, errno.ENOTDIR}
                else "queued directory could not be safely opened for inventory collection"
            )
            raise DirectorySafetyError(error.errno or errno.EIO, message, os.fspath(root)) from error
        self._root_descriptor = descriptor
        try:
            validate_directory_snapshot_identity(
                root,
                _descriptor_snapshot(root, descriptor),
                self._root_snapshot.snapshot,
            )
        except BaseException:
            os.close(descriptor)
            self._root_descriptor = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_value, traceback
        root_descriptor = self._root_descriptor
        try:
            if exc_type is None and root_descriptor is not None:
                self.finish_parent()
                self._validate_parent_mapping((), root_descriptor)
        finally:
            self._active_parent = None
            self._close_directory_cache()
            if root_descriptor is not None:
                os.close(root_descriptor)
                self._root_descriptor = None
        return None

    def read(self, relative: PurePosixPath, *, display_path: Path) -> StatSnapshot:
        """Read one path without following its final or intermediate links."""

        root = self._root_snapshot.path
        # Match lstat's one-time path materialization so later provenance and
        # repository checks reuse pathlib's cached representation.
        display = os.fspath(display_path)
        parts = relative.parts
        if not parts:
            descriptor = self._activate_parent((), display_path=display_path)
            return _descriptor_snapshot(root, descriptor)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise DirectorySafetyError(
                errno.EINVAL,
                "inventory metadata path was not a normalized descendant",
                display,
            )
        parent_descriptor = self._activate_parent(parts[:-1], display_path=display_path)
        final_name = parts[-1]
        statx_reader = self._statx_reader
        if statx_reader is not None and statx_reader.available:
            try:
                return statx_reader.read_snapshot_at(parent_descriptor, final_name, display_path=display_path)
            except OSError as error:
                snapshot = os.stat(final_name, dir_fd=parent_descriptor, follow_symlinks=False)
                return statx_fallback_snapshot(snapshot, error)
        return os.stat(final_name, dir_fd=parent_descriptor, follow_symlinks=False)

    def _activate_parent(self, parts: tuple[str, ...], *, display_path: Path) -> int:
        """Lease one parent descriptor while adjacent inventory entries use it."""

        active = self._active_parent
        if active is not None and active[0] == parts:
            return active[1]
        self.finish_parent()
        descriptor = self._parent_descriptor(parts, display_path=display_path)
        self._validate_parent_mapping(parts, descriptor)
        self._active_parent = (parts, descriptor)
        return descriptor

    def finish_parent(self) -> None:
        """Validate and release the active lexical parent lease."""

        active = self._active_parent
        if active is None:
            return
        self._active_parent = None
        parts, descriptor = active
        self._validate_parent_mapping(parts, descriptor)

    def _validate_parent_mapping(self, parts: tuple[str, ...], expected_descriptor: int) -> None:
        """Compare a cached handle with a fresh no-follow walk of its path."""

        root = self._root_snapshot.path
        path = root.joinpath(*parts)
        actual_descriptor: int | None = None
        try:
            actual_descriptor = os.open(root, _directory_open_flags())
            validate_directory_snapshot_identity(
                root,
                _descriptor_snapshot(root, actual_descriptor),
                self._root_snapshot.snapshot,
            )
            for part in parts:
                previous_descriptor = actual_descriptor
                actual_descriptor = os.open(part, _directory_open_flags(), dir_fd=previous_descriptor)
                os.close(previous_descriptor)
            validate_directory_snapshot_identity(
                path,
                _descriptor_snapshot(path, actual_descriptor),
                _descriptor_snapshot(path, expected_descriptor),
            )
        except DirectorySafetyError:
            self._invalidate_directory_cache()
            raise
        except OSError as error:
            self._invalidate_directory_cache()
            raise DirectorySafetyError(
                error.errno or errno.EIO,
                "inventory metadata ancestor could not be revalidated against its selected path",
                os.fspath(path),
            ) from error
        finally:
            if actual_descriptor is not None:
                os.close(actual_descriptor)

    def _parent_descriptor(self, parts: tuple[str, ...], *, display_path: Path) -> int:
        if not parts:
            return self._require_root_descriptor()

        prefix_size = len(parts)
        descriptor: int | None = None
        while prefix_size:
            prefix = parts[:prefix_size]
            descriptor = self._directories.get(prefix)
            if descriptor is not None:
                self._directories.move_to_end(prefix)
                break
            prefix_size -= 1
        if descriptor is None:
            descriptor = self._require_root_descriptor()

        for index in range(prefix_size, len(parts)):
            prefix = parts[: index + 1]
            try:
                descriptor = os.open(parts[index], _directory_open_flags(), dir_fd=descriptor)
            except OSError as error:
                raise DirectorySafetyError(
                    error.errno or errno.EIO,
                    "inventory metadata ancestor could not be opened without following symbolic links",
                    os.fspath(display_path),
                ) from error
            self._directories[prefix] = descriptor
            self._directories.move_to_end(prefix)
            self._trim_cache()
        return descriptor

    def _trim_cache(self) -> None:
        while len(self._directories) > self._cache_limit:
            _prefix, descriptor = self._directories.popitem(last=False)
            os.close(descriptor)

    def _invalidate_directory_cache(self) -> None:
        self._active_parent = None
        self._close_directory_cache()

    def _close_directory_cache(self) -> None:
        for descriptor in self._directories.values():
            os.close(descriptor)
        self._directories.clear()

    def _require_root_descriptor(self) -> int:
        if self._root_descriptor is None:
            raise RuntimeError("inventory metadata session is not open")
        return self._root_descriptor


def _directory_open_flags() -> int:
    access_mode = getattr(os, "O_PATH", os.O_RDONLY)
    return access_mode | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)


def _descriptor_snapshot(path: Path, descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError as error:
        raise DirectorySafetyError(
            error.errno or errno.EIO,
            "opened directory could not be revalidated during inventory collection",
            os.fspath(path),
        ) from error


__all__ = ["AnchoredInventoryMetadata", "anchored_inventory_metadata_supported"]
