from __future__ import annotations

import errno
from dataclasses import dataclass
from pathlib import Path

import pytest
from workfold.collection.filesystem.linux import LinuxStatxBirthTime
from workfold.collection.filesystem.metadata import FilesystemTimestampAdapter, TimestampExtraction
from workfold.domain.coverage import CapabilityStatus, ExtractionDisposition
from workfold.domain.observations import TimestampKind


@dataclass
class NanosecondSnapshot:
    st_mtime_ns: int | None = 1_700_000_000_000_000_001
    st_atime_ns: int | None = 1_700_000_000_000_000_002
    st_ctime_ns: int | None = 1_700_000_000_000_000_003
    st_dev: int = 17
    st_ino: int = 23


@dataclass
class FixedLinuxBirthReader:
    result: LinuxStatxBirthTime | None = None
    error: OSError | None = None
    available: bool = True

    def read(self, path: Path) -> LinuxStatxBirthTime:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError(f"no fixed statx result for {path}")
        return self.result


def test_nanosecond_fields_are_preserved_without_a_second_stat() -> None:
    adapter = FilesystemTimestampAdapter(
        platform_name="linux",
        created_supported=False,
        metadata_changed_supported=True,
    )
    snapshot = NanosecondSnapshot()

    modified = adapter.extract(snapshot, TimestampKind.FS_MODIFIED)
    changed = adapter.extract(snapshot, TimestampKind.FS_METADATA_CHANGED)
    accessed = adapter.extract(snapshot, TimestampKind.FS_ACCESSED)

    assert modified.instant_utc_ns == snapshot.st_mtime_ns
    assert modified.raw_timestamp == f"st_mtime_ns={snapshot.st_mtime_ns}"
    assert changed.instant_utc_ns == snapshot.st_ctime_ns
    assert changed.field_name == "st_ctime_ns"
    assert accessed.instant_utc_ns == snapshot.st_atime_ns
    assert accessed.note == "potentially unreliable"


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        (TimestampKind.FS_CREATED, CapabilityStatus.UNSUPPORTED),
        (TimestampKind.FS_MODIFIED, CapabilityStatus.SUPPORTED),
        (TimestampKind.FS_METADATA_CHANGED, CapabilityStatus.SUPPORTED),
        (TimestampKind.FS_ACCESSED, CapabilityStatus.POTENTIALLY_UNRELIABLE),
    ],
)
def test_capabilities_state_platform_semantics(
    kind: TimestampKind,
    expected_status: CapabilityStatus,
) -> None:
    adapter = FilesystemTimestampAdapter(
        platform_name="linux",
        created_supported=False,
        metadata_changed_supported=True,
    )
    capability = adapter.capability(kind, target="/fixture")

    assert capability.status is expected_status
    assert capability.timestamp_kind is kind
    if expected_status is CapabilityStatus.UNSUPPORTED:
        assert capability.note is not None and "statx" in capability.note
    if expected_status is CapabilityStatus.POTENTIALLY_UNRELIABLE:
        assert capability.note is not None and "atime" in capability.note


def test_posix_never_substitutes_ctime_for_creation() -> None:
    adapter = FilesystemTimestampAdapter(
        platform_name="linux",
        created_supported=False,
        metadata_changed_supported=True,
    )
    outcome = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED)

    assert outcome.disposition is ExtractionDisposition.UNSUPPORTED
    assert outcome.instant_utc_ns is None
    assert outcome.raw_timestamp is None
    assert outcome.note is not None and "statx" in outcome.note


def test_linux_statx_birth_time_is_preserved_with_identity_and_provenance() -> None:
    instant = 1_700_000_000_123_456_789
    reader = FixedLinuxBirthReader(LinuxStatxBirthTime(instant, 17, 23))
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    capability = adapter.capability(TimestampKind.FS_CREATED, target="/fixture")
    outcome = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED, path=Path("/fixture"))

    assert adapter.supports(TimestampKind.FS_CREATED)
    assert capability.status is CapabilityStatus.SUPPORTED
    assert capability.note == "Linux statx exposes STATX_BTIME"
    assert outcome.disposition is ExtractionDisposition.CAPTURED
    assert outcome.instant_utc_ns == instant
    assert outcome.field_name == "statx.stx_btime_ns"
    assert outcome.raw_timestamp == f"statx.stx_btime_ns={instant}"


def test_linux_statx_missing_birth_field_is_unavailable_per_entry() -> None:
    reader = FixedLinuxBirthReader(LinuxStatxBirthTime(None, 17, 23))
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    capability = adapter.capability(TimestampKind.FS_CREATED, target="/fixture")
    outcome = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED, path=Path("/fixture"))

    assert capability.status is CapabilityStatus.SUPPORTED
    assert capability.note is not None and "does not expose STATX_BTIME" in capability.note
    assert outcome.disposition is ExtractionDisposition.UNAVAILABLE
    assert outcome.field_name == "statx.stx_btime_ns"
    assert outcome.note is not None and "did not return STATX_BTIME" in outcome.note


def test_linux_statx_runtime_unavailability_is_not_a_parse_error() -> None:
    reader = FixedLinuxBirthReader(error=OSError(errno.ENOSYS, "not implemented"))
    adapter = FilesystemTimestampAdapter(platform_name="linux", linux_birthtime_reader=reader)

    capability = adapter.capability(TimestampKind.FS_CREATED, target="/fixture")
    outcome = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED, path=Path("/fixture"))

    assert capability.status is CapabilityStatus.UNSUPPORTED
    assert outcome.disposition is ExtractionDisposition.UNSUPPORTED
    assert outcome.note is not None and "does not provide statx" in outcome.note


def test_linux_statx_read_failure_and_identity_change_are_errors() -> None:
    denied = FilesystemTimestampAdapter(
        platform_name="linux",
        linux_birthtime_reader=FixedLinuxBirthReader(error=PermissionError(errno.EACCES, "denied")),
    )
    denied_capability = denied.capability(TimestampKind.FS_CREATED, target="/fixture")
    denied_outcome = denied.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED, path=Path("/fixture"))
    changed = FilesystemTimestampAdapter(
        platform_name="linux",
        linux_birthtime_reader=FixedLinuxBirthReader(LinuxStatxBirthTime(1_700_000_000_000_000_000, 99, 100)),
    ).extract(NanosecondSnapshot(), TimestampKind.FS_CREATED, path=Path("/fixture"))

    assert denied_capability.status is CapabilityStatus.UNAVAILABLE
    assert denied_outcome.disposition is ExtractionDisposition.ERROR
    assert denied_outcome.note is not None and "read failed" in denied_outcome.note
    assert changed.disposition is ExtractionDisposition.ERROR
    assert changed.note is not None and "entry changed" in changed.note


def test_linux_statx_birth_time_requires_a_path() -> None:
    adapter = FilesystemTimestampAdapter(
        platform_name="linux",
        linux_birthtime_reader=FixedLinuxBirthReader(LinuxStatxBirthTime(1, 17, 23)),
    )

    outcome = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED)

    assert outcome.disposition is ExtractionDisposition.ERROR
    assert outcome.note is not None and "requires the source path" in outcome.note


def test_real_birth_fields_are_used_when_exposed() -> None:
    class BirthNanoseconds:
        st_birthtime_ns = 1_234_567_890

    class BirthSeconds:
        st_birthtime = 1.23456789

    adapter = FilesystemTimestampAdapter(platform_name="darwin", created_supported=True)

    nanoseconds = adapter.extract(BirthNanoseconds(), TimestampKind.FS_CREATED)
    seconds = adapter.extract(BirthSeconds(), TimestampKind.FS_CREATED)

    assert nanoseconds.instant_utc_ns == 1_234_567_890
    assert nanoseconds.raw_timestamp == "st_birthtime_ns=1234567890"
    assert seconds.instant_utc_ns == 1_234_567_890
    assert seconds.raw_timestamp == "st_birthtime=1234567890"


def test_windows_creation_fallback_does_not_claim_metadata_change() -> None:
    adapter = FilesystemTimestampAdapter(
        platform_name="win32",
        created_supported=True,
        metadata_changed_supported=False,
    )
    created = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_CREATED)
    changed = adapter.extract(NanosecondSnapshot(), TimestampKind.FS_METADATA_CHANGED)

    assert adapter.is_windows
    assert created.field_name == "st_ctime_ns"
    assert created.instant_utc_ns == NanosecondSnapshot().st_ctime_ns
    assert changed.disposition is ExtractionDisposition.UNSUPPORTED
    assert changed.note is not None and "distinct" in changed.note


def test_supported_but_missing_field_is_unavailable() -> None:
    class MissingFields:
        st_birthtime_ns = None

    outcome = FilesystemTimestampAdapter(
        platform_name="darwin",
        created_supported=True,
    ).extract(MissingFields(), TimestampKind.FS_CREATED)

    assert outcome.disposition is ExtractionDisposition.UNAVAILABLE
    assert outcome.field_name == "st_birthtime_ns"
    assert outcome.note is not None and "no value" in outcome.note


@pytest.mark.parametrize("value", [1.5, True, "1700000000000000000"])
def test_invalid_exposed_nanosecond_fields_are_errors(value: object) -> None:
    class InvalidSnapshot:
        st_mtime_ns = value

    outcome = FilesystemTimestampAdapter().extract(InvalidSnapshot(), TimestampKind.FS_MODIFIED)

    assert outcome.disposition is ExtractionDisposition.ERROR
    assert outcome.note is not None and "could not parse" in outcome.note


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number"])
def test_invalid_birth_seconds_are_errors(value: object) -> None:
    class InvalidBirth:
        st_birthtime = value

    outcome = FilesystemTimestampAdapter(
        platform_name="darwin",
        created_supported=True,
    ).extract(InvalidBirth(), TimestampKind.FS_CREATED)

    assert outcome.disposition is ExtractionDisposition.ERROR


@pytest.mark.parametrize("value", [-(10**30), 10**30])
def test_unlocalizable_instants_are_extraction_errors(value: int) -> None:
    class ExtremeSnapshot:
        st_mtime_ns = value

    outcome = FilesystemTimestampAdapter().extract(ExtremeSnapshot(), TimestampKind.FS_MODIFIED)

    assert outcome.disposition is ExtractionDisposition.ERROR
    assert outcome.note is not None and "safely localizable" in outcome.note


def test_extraction_result_enforces_captured_value_invariant() -> None:
    with pytest.raises(ValueError, match="require an instant"):
        TimestampExtraction(TimestampKind.FS_MODIFIED, ExtractionDisposition.CAPTURED)
    with pytest.raises(ValueError, match="cannot carry"):
        TimestampExtraction(
            TimestampKind.FS_MODIFIED,
            ExtractionDisposition.ERROR,
            instant_utc_ns=1,
            raw_timestamp="bad",
        )


def test_adapter_rejects_non_filesystem_timestamp_kinds() -> None:
    adapter = FilesystemTimestampAdapter()
    with pytest.raises(ValueError, match="not a filesystem"):
        adapter.supports(TimestampKind.GIT_AUTHOR)
    with pytest.raises(ValueError, match="not a filesystem"):
        adapter.capability(TimestampKind.GIT_COMMITTER, target="/fixture")
    with pytest.raises(ValueError, match="not a filesystem"):
        adapter.extract(NanosecondSnapshot(), TimestampKind.GIT_TAGGER)
