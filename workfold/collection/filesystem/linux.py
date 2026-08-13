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


@dataclass(frozen=True, slots=True)
class LinuxStatxBirthTime:
    """Birth-time value and identity returned by one no-follow ``statx`` call."""

    instant_utc_ns: int | None
    device_id: int
    inode: int | None


class LinuxStatxReader:
    """Read Linux birth time through libc without invoking an external command."""

    __slots__ = ("_function", "_library")

    def __init__(self) -> None:
        self._library: ctypes.CDLL | None = None
        self._function: Any | None = None
        try:
            library = ctypes.CDLL(None, use_errno=True)
            function = getattr(library, "statx")
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

        function = self._function
        if function is None:
            raise OSError(errno.ENOSYS, "libc does not expose statx", os.fspath(path))

        buffer = _Statx()
        ctypes.set_errno(0)
        result = function(
            _AT_FDCWD,
            os.fsencode(path),
            _AT_SYMLINK_NOFOLLOW | _AT_NO_AUTOMOUNT,
            _STATX_BASIC_STATS | _STATX_BTIME,
            ctypes.byref(buffer),
        )
        if result != 0:
            error_number = ctypes.get_errno() or errno.EIO
            raise OSError(error_number, os.strerror(error_number), os.fspath(path))

        instant_utc_ns: int | None = None
        if buffer.stx_mask & _STATX_BTIME:
            nanoseconds = int(buffer.stx_btime.tv_nsec)
            if not 0 <= nanoseconds < _NANOSECONDS_PER_SECOND:
                raise OSError(
                    errno.EIO,
                    f"statx returned an invalid birth-time nanosecond field: {nanoseconds}",
                    os.fspath(path),
                )
            instant_utc_ns = int(buffer.stx_btime.tv_sec) * _NANOSECONDS_PER_SECOND + nanoseconds

        inode = int(buffer.stx_ino) if buffer.stx_mask & _STATX_INO else None
        device_id = os.makedev(int(buffer.stx_dev_major), int(buffer.stx_dev_minor))
        return LinuxStatxBirthTime(instant_utc_ns, device_id, inode)


__all__ = ["LinuxStatxBirthTime", "LinuxStatxReader"]
