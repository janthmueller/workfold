"""Lossless parsing of Git identity and timestamp signatures."""

from __future__ import annotations

from typing import Final

from wuf.collection.git.objects.models import GitIdentity, GitObjectParseError, GitSignature

_MIN_LOCALIZABLE_EPOCH_SECONDS: Final[int] = -62_135_510_400
_MAX_LOCALIZABLE_EPOCH_SECONDS: Final[int] = 253_402_214_399


def decode_losslessly(value: bytes) -> str:
    """Decode source bytes without discarding non-UTF-8 values."""

    return value.decode("utf-8", errors="surrogateescape")


def parse_git_epoch(epoch_raw: bytes, *, role: str, object_id: str) -> int:
    """Parse one Git epoch while enforcing Wuf's localizable range."""

    epoch_digits = epoch_raw[1:] if epoch_raw.startswith(b"-") else epoch_raw
    if not epoch_digits or not epoch_digits.isdigit():
        raise GitObjectParseError(
            "invalid_git_timestamp",
            f"{role} header has an invalid epoch",
            object_id=object_id,
        )
    try:
        epoch_seconds = int(epoch_raw)
    except ValueError as error:
        raise GitObjectParseError(
            "invalid_git_timestamp",
            f"{role} header epoch cannot be represented",
            object_id=object_id,
        ) from error
    if not _MIN_LOCALIZABLE_EPOCH_SECONDS <= epoch_seconds <= _MAX_LOCALIZABLE_EPOCH_SECONDS:
        raise GitObjectParseError(
            "invalid_git_timestamp",
            f"{role} header epoch is outside Wuf's localizable datetime range",
            object_id=object_id,
        )
    return epoch_seconds


def parse_git_signature(value: bytes, *, role: str, object_id: str) -> GitSignature:
    """Parse one raw Git identity/epoch/offset signature without formatting loss."""

    identity_raw, epoch_raw, offset_raw = _split_git_signature(value, role=role, object_id=object_id)
    epoch_seconds, offset_seconds = _parse_git_timestamp(
        epoch_raw,
        offset_raw,
        role=role,
        object_id=object_id,
    )
    identity_separator = identity_raw.rfind(b" <")
    raw_name = identity_raw[:identity_separator]
    raw_email = identity_raw[identity_separator + 2 : -1]
    timestamp_raw = epoch_raw + b" " + offset_raw
    identity = GitIdentity(
        name=decode_losslessly(raw_name),
        email=decode_losslessly(raw_email),
        raw=identity_raw,
        raw_name=raw_name,
        raw_email=raw_email,
    )
    return GitSignature(
        identity=identity,
        epoch_seconds=epoch_seconds,
        offset_seconds=offset_seconds,
        raw=value,
        raw_timestamp=timestamp_raw.decode("ascii"),
        raw_timestamp_bytes=timestamp_raw,
        raw_offset=offset_raw.decode("ascii"),
    )


def _split_git_signature(
    value: bytes,
    *,
    role: str,
    object_id: str,
) -> tuple[bytes, bytes, bytes]:
    try:
        identity_raw, epoch_raw, offset_raw = value.rsplit(b" ", 2)
    except ValueError as error:
        raise GitObjectParseError(
            "invalid_git_signature",
            f"{role} header does not end in an epoch and UTC offset",
            object_id=object_id,
        ) from error

    identity_separator = identity_raw.rfind(b" <")
    if identity_separator < 0 or not identity_raw.endswith(b">"):
        raise GitObjectParseError(
            "invalid_git_identity",
            f"{role} header has no valid name/email identity",
            object_id=object_id,
        )
    return identity_raw, epoch_raw, offset_raw


def _parse_git_timestamp(
    epoch_raw: bytes,
    offset_raw: bytes,
    *,
    role: str,
    object_id: str,
) -> tuple[int, int]:
    epoch_seconds = parse_git_epoch(epoch_raw, role=role, object_id=object_id)
    if len(offset_raw) != 5 or offset_raw[:1] not in {b"+", b"-"} or not offset_raw[1:].isdigit():
        raise GitObjectParseError(
            "invalid_git_timestamp",
            f"{role} header has an invalid UTC offset",
            object_id=object_id,
        )

    offset_hours = int(offset_raw[1:3])
    offset_minutes = int(offset_raw[3:5])
    if offset_hours > 23 or offset_minutes > 59:
        raise GitObjectParseError(
            "invalid_git_timestamp",
            f"{role} header has an out-of-range UTC offset",
            object_id=object_id,
        )

    sign = 1 if offset_raw[0:1] == b"+" else -1
    return epoch_seconds, sign * (offset_hours * 3_600 + offset_minutes * 60)


__all__ = ["decode_losslessly", "parse_git_epoch", "parse_git_signature"]
