"""Platform-honest filesystem timestamp extraction.

Portable fields come from one already captured no-follow stat snapshot. Linux
birth time uses a companion no-follow ``statx`` read because Python's ordinary
``os.stat_result`` omits ``STATX_BTIME``; identity is checked before combining
the two results. POSIX ``ctime`` is never labeled as creation time.
"""

from __future__ import annotations

import errno
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Protocol

from workfold.collection.filesystem.linux import LinuxStatxBirthTime, LinuxStatxReader
from workfold.domain.coverage import Capability, CapabilityStatus, ExtractionDisposition
from workfold.domain.observations import Source, TimestampKind

_NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000
# Keep one UTC day away from datetime's absolute endpoints.  This leaves room
# for all legal timezone offsets during later selected-zone conversion.
_MIN_LOCALIZABLE_NS: Final[int] = -62_135_510_400 * _NANOSECONDS_PER_SECOND
_MAX_LOCALIZABLE_NS: Final[int] = 253_402_214_400 * _NANOSECONDS_PER_SECOND
_FILESYSTEM_KINDS: Final[frozenset[TimestampKind]] = frozenset(
    {
        TimestampKind.FS_CREATED,
        TimestampKind.FS_MODIFIED,
        TimestampKind.FS_METADATA_CHANGED,
        TimestampKind.FS_ACCESSED,
    }
)


class LinuxBirthTimeReader(Protocol):
    """Injectable boundary for Linux birth-time collection."""

    @property
    def available(self) -> bool: ...

    def read(self, path: Path) -> LinuxStatxBirthTime: ...


def _default_linux_birthtime_reader() -> LinuxBirthTimeReader | None:
    return LinuxStatxReader() if sys.platform.startswith("linux") else None


@dataclass(frozen=True, slots=True)
class TimestampExtraction:
    """The terminal extraction outcome for one requested timestamp slot."""

    kind: TimestampKind
    disposition: ExtractionDisposition
    instant_utc_ns: int | None = None
    raw_timestamp: str | None = None
    field_name: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        captured = self.disposition is ExtractionDisposition.CAPTURED
        if captured and (self.instant_utc_ns is None or self.raw_timestamp is None):
            raise ValueError("captured timestamp outcomes require an instant and raw value")
        if not captured and (self.instant_utc_ns is not None or self.raw_timestamp is not None):
            raise ValueError("non-captured timestamp outcomes cannot carry a timestamp")


@dataclass(frozen=True, slots=True)
class FilesystemTimestampAdapter:
    """Describe and extract real timestamp fields exposed by platform APIs.

    ``platform_name``, the Linux reader, and the capability overrides are
    injectable solely to make behavior testable on every CI host. Production
    callers use Python stat fields plus libc ``statx`` on Linux.
    """

    platform_name: str = sys.platform
    created_supported: bool | None = None
    metadata_changed_supported: bool | None = None
    linux_birthtime_reader: LinuxBirthTimeReader | None = field(default_factory=_default_linux_birthtime_reader)

    @property
    def is_windows(self) -> bool:
        """Return whether Windows timestamp semantics apply."""

        return self.platform_name == "win32"

    @property
    def is_linux(self) -> bool:
        """Return whether Linux ``statx`` semantics apply."""

        return self.platform_name.startswith("linux")

    def supports(self, kind: TimestampKind) -> bool:
        """Return whether the runtime exposes a real platform API for ``kind``."""

        _require_filesystem_kind(kind)
        if kind is TimestampKind.FS_CREATED:
            if self.created_supported is not None:
                return self.created_supported
            if self.is_windows:
                return True
            if self.is_linux:
                python_exposes_birthtime = self.platform_name == sys.platform and (
                    _stat_result_has("st_birthtime_ns") or _stat_result_has("st_birthtime")
                )
                return python_exposes_birthtime or (
                    self.linux_birthtime_reader is not None and self.linux_birthtime_reader.available
                )
            return _stat_result_has("st_birthtime_ns") or _stat_result_has("st_birthtime")
        if kind is TimestampKind.FS_METADATA_CHANGED:
            if self.metadata_changed_supported is not None:
                return self.metadata_changed_supported
            return not self.is_windows and _stat_result_has("st_ctime_ns")
        if kind is TimestampKind.FS_MODIFIED:
            return _stat_result_has("st_mtime_ns")
        return _stat_result_has("st_atime_ns")

    def capability(self, kind: TimestampKind, *, target: str) -> Capability:
        """Build the typed capability statement for one requested kind."""

        if kind is TimestampKind.FS_CREATED and self._uses_linux_statx() and self.created_supported is not False:
            return self._linux_birthtime_capability(Path(target))
        supported = self.supports(kind)
        if kind is TimestampKind.FS_ACCESSED and supported:
            return Capability(
                source=Source.FILESYSTEM,
                target=target,
                name="filesystem accessed time",
                status=CapabilityStatus.POTENTIALLY_UNRELIABLE,
                timestamp_kind=kind,
                note="atime may be disabled, delayed, or changed by reads",
            )
        note: str | None = None
        if not supported:
            note = _unsupported_note(kind, is_windows=self.is_windows, is_linux=self.is_linux)
        return Capability(
            source=Source.FILESYSTEM,
            target=target,
            name=_capability_name(kind),
            status=CapabilityStatus.SUPPORTED if supported else CapabilityStatus.UNSUPPORTED,
            timestamp_kind=kind,
            note=note,
        )

    def extract(self, snapshot: object, kind: TimestampKind, *, path: Path | None = None) -> TimestampExtraction:
        """Extract one kind, using ``path`` only for Linux birth time."""

        _require_filesystem_kind(kind)
        if not self.supports(kind):
            return TimestampExtraction(
                kind,
                ExtractionDisposition.UNSUPPORTED,
                note=_unsupported_note(kind, is_windows=self.is_windows, is_linux=self.is_linux),
            )

        if kind is TimestampKind.FS_CREATED and self._uses_linux_statx(snapshot):
            return self._extract_linux_birthtime(snapshot, path)

        try:
            field_name, value = self._read_field(snapshot, kind)
        except (AttributeError, InvalidOperation, TypeError, ValueError, OverflowError) as error:
            return TimestampExtraction(
                kind,
                ExtractionDisposition.ERROR,
                note=f"could not parse the exposed timestamp field: {error}",
            )
        return _finish_extraction(
            kind,
            field_name,
            value,
            unavailable_note="the platform supports this timestamp kind but this entry exposes no value",
        )

    def _uses_linux_statx(self, snapshot: object | None = None) -> bool:
        if not self.is_linux:
            return False
        if snapshot is None and self.platform_name != sys.platform:
            return True
        target = os.stat_result if snapshot is None else snapshot
        return not hasattr(target, "st_birthtime_ns") and not hasattr(target, "st_birthtime")

    def _linux_birthtime_capability(self, target: Path) -> Capability:
        reader = self.linux_birthtime_reader
        if reader is None or not reader.available:
            return Capability(
                source=Source.FILESYSTEM,
                target=os.fspath(target),
                name=_capability_name(TimestampKind.FS_CREATED),
                status=CapabilityStatus.UNSUPPORTED,
                timestamp_kind=TimestampKind.FS_CREATED,
                note=_unsupported_note(TimestampKind.FS_CREATED, is_windows=False, is_linux=True),
            )
        try:
            result = reader.read(target)
        except OSError as error:
            unsupported = error.errno == errno.ENOSYS
            return Capability(
                source=Source.FILESYSTEM,
                target=os.fspath(target),
                name=_capability_name(TimestampKind.FS_CREATED),
                status=CapabilityStatus.UNSUPPORTED if unsupported else CapabilityStatus.UNAVAILABLE,
                timestamp_kind=TimestampKind.FS_CREATED,
                note=(
                    "the Linux runtime does not provide statx"
                    if unsupported
                    else f"the Linux statx capability probe failed: {error}"
                ),
            )
        note = "Linux statx exposes STATX_BTIME"
        if result.instant_utc_ns is None:
            note = "Linux statx is available, but this root does not expose STATX_BTIME"
        return Capability(
            source=Source.FILESYSTEM,
            target=os.fspath(target),
            name=_capability_name(TimestampKind.FS_CREATED),
            status=CapabilityStatus.SUPPORTED,
            timestamp_kind=TimestampKind.FS_CREATED,
            note=note,
        )

    def _extract_linux_birthtime(self, snapshot: object, path: Path | None) -> TimestampExtraction:
        kind = TimestampKind.FS_CREATED
        reader = self.linux_birthtime_reader
        if path is None:
            return TimestampExtraction(
                kind,
                ExtractionDisposition.ERROR,
                note="Linux birth-time extraction requires the source path",
            )
        if reader is None or not reader.available:
            return TimestampExtraction(
                kind,
                ExtractionDisposition.UNSUPPORTED,
                note=_unsupported_note(kind, is_windows=False, is_linux=True),
            )
        try:
            result = reader.read(path)
        except OSError as error:
            if error.errno == errno.ENOSYS:
                return TimestampExtraction(
                    kind,
                    ExtractionDisposition.UNSUPPORTED,
                    note="the Linux runtime does not provide statx",
                )
            return TimestampExtraction(
                kind,
                ExtractionDisposition.ERROR,
                note=f"Linux statx birth-time read failed: {error}",
            )

        try:
            snapshot_device = _integer_field(snapshot, "st_dev")
            snapshot_inode = _integer_field(snapshot, "st_ino")
        except TypeError as error:
            return TimestampExtraction(
                kind,
                ExtractionDisposition.ERROR,
                note=f"could not validate the Linux statx entry identity: {error}",
            )
        if (
            snapshot_device is not None
            and snapshot_inode is not None
            and result.inode is not None
            and (snapshot_device, snapshot_inode) != (result.device_id, result.inode)
        ):
            return TimestampExtraction(
                kind,
                ExtractionDisposition.ERROR,
                note="filesystem entry changed between the no-follow stat and statx reads",
            )
        return _finish_extraction(
            kind,
            "statx.stx_btime_ns",
            result.instant_utc_ns,
            unavailable_note="the filesystem did not return STATX_BTIME for this entry",
        )

    def _read_field(self, snapshot: object, kind: TimestampKind) -> tuple[str, int | None]:
        if kind is TimestampKind.FS_MODIFIED:
            return "st_mtime_ns", _integer_field(snapshot, "st_mtime_ns")
        if kind is TimestampKind.FS_ACCESSED:
            return "st_atime_ns", _integer_field(snapshot, "st_atime_ns")
        if kind is TimestampKind.FS_METADATA_CHANGED:
            return "st_ctime_ns", _integer_field(snapshot, "st_ctime_ns")

        # Modern Windows and BSD/macOS Python expose an unambiguous birth field.
        if hasattr(snapshot, "st_birthtime_ns"):
            return "st_birthtime_ns", _integer_field(snapshot, "st_birthtime_ns")
        if hasattr(snapshot, "st_birthtime"):
            return "st_birthtime", _seconds_field(snapshot, "st_birthtime")
        # On older Windows versions only, Python documents st_ctime as creation
        # time.  This branch is forbidden on POSIX by ``supports`` above.
        if self.is_windows:
            return "st_ctime_ns", _integer_field(snapshot, "st_ctime_ns")
        return "st_birthtime_ns", None


def _stat_result_has(field_name: str) -> bool:
    return hasattr(os.stat_result, field_name)


def _integer_field(snapshot: object, field_name: str) -> int | None:
    value = getattr(snapshot, field_name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} is not an integer")
    return value


def _seconds_field(snapshot: object, field_name: str) -> int | None:
    value = getattr(snapshot, field_name, None)
    if value is None:
        return None
    # Decimal(str(...)) preserves exactly the value Python exposed rather than
    # adding another binary-float multiplication artifact.
    seconds = Decimal(str(value))
    return int(seconds * _NANOSECONDS_PER_SECOND)


def _require_filesystem_kind(kind: TimestampKind) -> None:
    if kind not in _FILESYSTEM_KINDS:
        raise ValueError(f"not a filesystem timestamp kind: {kind.value}")


def _capability_name(kind: TimestampKind) -> str:
    return {
        TimestampKind.FS_CREATED: "filesystem creation/birth time",
        TimestampKind.FS_MODIFIED: "filesystem modification time",
        TimestampKind.FS_METADATA_CHANGED: "filesystem metadata-change time",
        TimestampKind.FS_ACCESSED: "filesystem accessed time",
    }[kind]


def _finish_extraction(
    kind: TimestampKind,
    field_name: str,
    value: int | None,
    *,
    unavailable_note: str,
) -> TimestampExtraction:
    if value is None:
        return TimestampExtraction(
            kind,
            ExtractionDisposition.UNAVAILABLE,
            field_name=field_name,
            note=unavailable_note,
        )
    if not _MIN_LOCALIZABLE_NS <= value < _MAX_LOCALIZABLE_NS:
        return TimestampExtraction(
            kind,
            ExtractionDisposition.ERROR,
            field_name=field_name,
            note="timestamp is outside Workfold's safely localizable datetime range",
        )
    return TimestampExtraction(
        kind,
        ExtractionDisposition.CAPTURED,
        instant_utc_ns=value,
        raw_timestamp=f"{field_name}={value}",
        field_name=field_name,
        note="potentially unreliable" if kind is TimestampKind.FS_ACCESSED else None,
    )


def _unsupported_note(kind: TimestampKind, *, is_windows: bool, is_linux: bool) -> str:
    if kind is TimestampKind.FS_CREATED:
        if is_linux:
            return "Linux statx birth-time access is unavailable in this runtime"
        return "Python exposes no real creation/birth-time field on this platform"
    if kind is TimestampKind.FS_METADATA_CHANGED and is_windows:
        return "Python exposes no metadata-change time distinct from Windows creation time"
    return "Python exposes no nanosecond field for this timestamp kind"


__all__ = ["FilesystemTimestampAdapter", "LinuxBirthTimeReader", "TimestampExtraction"]
