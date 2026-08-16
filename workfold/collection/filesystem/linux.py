"""Minimal Linux ``statx`` binding for filesystem birth timestamps.

Python's ``os.stat_result`` does not currently expose Linux ``STATX_BTIME``.
This module binds only the libc ``statx`` surface Workfold needs and keeps the
platform-specific ABI out of the collector and normalized event model.
"""

from __future__ import annotations

import ctypes
import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_AT_FDCWD: Final[int] = -100
_AT_SYMLINK_NOFOLLOW: Final[int] = 0x100
_AT_NO_AUTOMOUNT: Final[int] = 0x800
_STATX_BASIC_STATS: Final[int] = 0x07FF
_STATX_TYPE: Final[int] = 0x0001
_STATX_MODE: Final[int] = 0x0002
_STATX_ATIME: Final[int] = 0x0020
_STATX_MTIME: Final[int] = 0x0040
_STATX_CTIME: Final[int] = 0x0080
_STATX_INO: Final[int] = 0x0100
_STATX_BTIME: Final[int] = 0x0800
_NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000
_STATX_BUFFER_SIZE: Final[int] = 0x100


class _StatxTimestamp(ctypes.Structure):
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # Linux's UAPI fixes this structure at 0x100 bytes. The explicit padding
    # keeps the binding independent of libc headers while retaining the exact
    # offsets of every field Workfold reads.
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("tail", ctypes.c_ubyte * (_STATX_BUFFER_SIZE - 0x90)),
    ]


if ctypes.sizeof(_Statx) != _STATX_BUFFER_SIZE:  # pragma: no cover - fixed Linux UAPI invariant
    raise RuntimeError("Workfold's Linux statx structure does not match the 256-byte UAPI layout")


class LinuxStatxCallError(OSError):
    """The ``statx`` syscall failed before returning usable metadata."""


@dataclass(frozen=True, slots=True)
class LinuxStatxBirthTime:
    """Birth-time value and identity returned by one no-follow ``statx`` call."""

    instant_utc_ns: int | None
    device_id: int
    inode: int | None


@dataclass(frozen=True, slots=True)
class LinuxStatxSnapshot:
    """No-follow metadata snapshot returned by one Linux ``statx`` call."""

    st_mode: int
    st_dev: int
    st_ino: int
    st_atime_ns: int
    st_mtime_ns: int
    st_ctime_ns: int
    birth_time_utc_ns: int | None


@dataclass(frozen=True, slots=True)
class LinuxStatxFallbackSnapshot:
    """Portable metadata paired with the failed combined ``statx`` call."""

    st_mode: int
    st_dev: int
    st_ino: int
    st_atime_ns: int
    st_mtime_ns: int
    st_ctime_ns: int
    statx_error_number: int | None
    statx_error_message: str


class LinuxStatxReader:
    """Read no-follow Linux metadata, including birth time, through libc."""

    __slots__ = ("_function", "_library")

    def __init__(self) -> None:
        self._library: ctypes.CDLL | None = None
        self._function: Any | None = None
        try:
            library = ctypes.CDLL(None, use_errno=True)
            function = getattr(library, "statx")  # noqa: B009 - optional libc symbol
        except (AttributeError, OSError):
            return
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Statx),
        ]
        function.restype = ctypes.c_int
        self._library = library
        self._function = function

    @property
    def available(self) -> bool:
        """Return whether this libc exports the Linux ``statx`` entry point."""

        return self._function is not None

    def read(self, path: Path) -> LinuxStatxBirthTime:
        """Read the directory entry itself and return birth time when supplied."""

        buffer = self._read_buffer(path)
        instant_utc_ns = _birth_time_ns(buffer, path)
        inode = int(buffer.stx_ino) if buffer.stx_mask & _STATX_INO else None
        device_id = os.makedev(int(buffer.stx_dev_major), int(buffer.stx_dev_minor))
        return LinuxStatxBirthTime(instant_utc_ns, device_id, inode)

    def read_snapshot(self, path: Path) -> LinuxStatxSnapshot:
        """Read all metadata Workfold needs without a companion ``lstat`` call."""

        buffer = self._read_buffer(path)
        return _snapshot(buffer, path)

    def read_snapshot_at(
        self,
        directory_fd: int,
        name: str | bytes,
        *,
        display_path: Path,
    ) -> LinuxStatxSnapshot:
        """Read one entry relative to an already-open directory descriptor."""

        buffer = self._read_buffer(
            display_path,
            directory_fd=directory_fd,
            encoded_path=os.fsencode(name),
        )
        return _snapshot(buffer, display_path)

    def _read_buffer(
        self,
        path: Path,
        *,
        directory_fd: int = _AT_FDCWD,
        encoded_path: bytes | None = None,
    ) -> _Statx:
        """Invoke ``statx`` once and return its result buffer."""

        function = self._function
        if function is None:
            raise OSError(errno.ENOSYS, "libc does not expose statx", os.fspath(path))

        buffer = _Statx()
        ctypes.set_errno(0)
        result = function(
            directory_fd,
            os.fsencode(path) if encoded_path is None else encoded_path,
            _AT_SYMLINK_NOFOLLOW | _AT_NO_AUTOMOUNT,
            _STATX_BASIC_STATS | _STATX_BTIME,
            ctypes.byref(buffer),
        )
        if result != 0:
            error_number = ctypes.get_errno() or errno.EIO
            if error_number == errno.ENOSYS:
                # The libc symbol can exist while an older kernel or sandbox
                # rejects the syscall. Remember that process-wide capability
                # result so later entries fall back without repeating it.
                self._function = None
            raise LinuxStatxCallError(error_number, os.strerror(error_number), os.fspath(path))

        return buffer


def _snapshot(buffer: _Statx, path: Path) -> LinuxStatxSnapshot:
    """Normalize one validated ``statx`` buffer into collector metadata."""

    required = _STATX_TYPE | _STATX_MODE | _STATX_INO | _STATX_ATIME | _STATX_MTIME | _STATX_CTIME
    missing = required & ~int(buffer.stx_mask)
    if missing:
        raise OSError(
            errno.EOPNOTSUPP,
            f"statx omitted required basic metadata (mask 0x{missing:x})",
            os.fspath(path),
        )
    return LinuxStatxSnapshot(
        st_mode=int(buffer.stx_mode),
        st_dev=os.makedev(int(buffer.stx_dev_major), int(buffer.stx_dev_minor)),
        st_ino=int(buffer.stx_ino),
        st_atime_ns=_timestamp_ns(buffer.stx_atime, "access time", path),
        st_mtime_ns=_timestamp_ns(buffer.stx_mtime, "modification time", path),
        st_ctime_ns=_timestamp_ns(buffer.stx_ctime, "metadata-change time", path),
        birth_time_utc_ns=_birth_time_ns(buffer, path),
    )


def _birth_time_ns(buffer: _Statx, path: Path) -> int | None:
    if not buffer.stx_mask & _STATX_BTIME:
        return None
    return _timestamp_ns(buffer.stx_btime, "birth time", path)


def _timestamp_ns(timestamp: _StatxTimestamp, label: str, path: Path) -> int:
    nanoseconds = int(timestamp.tv_nsec)
    if not 0 <= nanoseconds < _NANOSECONDS_PER_SECOND:
        raise OSError(
            errno.EIO,
            f"statx returned an invalid {label} nanosecond field: {nanoseconds}",
            os.fspath(path),
        )
    return int(timestamp.tv_sec) * _NANOSECONDS_PER_SECOND + nanoseconds


__all__ = [
    "LinuxStatxBirthTime",
    "LinuxStatxCallError",
    "LinuxStatxFallbackSnapshot",
    "LinuxStatxReader",
    "LinuxStatxSnapshot",
]
