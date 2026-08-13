"""Parsing for the minimal ASCII-only ``git rev-list`` scan."""

from __future__ import annotations

import re
from typing import Final

from workfold.collection.git.objects.models import (
    GitObjectParseError,
    RevListCommitScan,
    RevListScanSpec,
)
from workfold.collection.git.objects.signatures import parse_git_epoch

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def inspect_rev_list_scan(record: bytes, spec: RevListScanSpec) -> RevListCommitScan:
    """Parse one minimal commit-selection record emitted for ``spec``."""

    fields = _parse_rev_list_fields(record)
    expected_fields = 1 + len(spec.roles)
    if len(fields) != expected_fields:
        raise GitObjectParseError(
            "invalid_rev_list_record",
            f"rev-list scan record has {len(fields)} fields instead of {expected_fields}",
        )
    object_id_raw = fields[0]
    if not _OID_RE.fullmatch(object_id_raw):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list scan record has no valid commit object ID",
        )
    object_id = object_id_raw.decode("ascii")
    instants = tuple(
        parse_git_epoch(raw_epoch, role=role, object_id=object_id) * 1_000_000_000
        for role, raw_epoch in zip(spec.roles, fields[1:], strict=True)
    )
    return RevListCommitScan(object_id, spec.roles, instants)


def _parse_rev_list_fields(record: bytes) -> list[bytes]:
    if not record.endswith(b"\n"):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list scan record has no newline terminator",
        )
    payload = record[:-1]
    if not payload.endswith(b"\0"):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list scan record has no final field terminator",
        )
    return payload[:-1].split(b"\0")


__all__ = ["inspect_rev_list_scan"]
