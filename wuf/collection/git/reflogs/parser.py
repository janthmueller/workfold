"""Exact parsers for Git reflog discovery and record bytes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

from wuf.collection.git.reflogs.models import GitReflogParseError, ParsedReflogEntry

OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
EPOCH_RE: Final[re.Pattern[bytes]] = re.compile(rb"-?[0-9]+\Z")
OFFSET_RE: Final[re.Pattern[bytes]] = re.compile(rb"[+-][0-9]{4}\Z")
MIN_LOCALIZABLE_EPOCH: Final[int] = -62_135_510_400
MAX_LOCALIZABLE_EPOCH: Final[int] = 253_402_214_399


@dataclass(frozen=True, slots=True)
class ParsedReflogLine:
    old_id: bytes
    new_id: bytes
    identity: bytes
    epoch: bytes
    offset: bytes
    actor_name: bytes
    actor_email: bytes
    message: bytes
    epoch_seconds: int
    offset_seconds: int

    @property
    def duplicate_key(self) -> tuple[bytes, bytes, bytes, bytes, bytes]:
        return self.old_id, self.new_id, self.identity, self.epoch + b" " + self.offset, self.message

    def to_entry(
        self,
        *,
        ref_name: str,
        raw_ref_name: bytes,
        selector_index: int,
        duplicate_ordinal: int,
    ) -> ParsedReflogEntry:
        raw_timestamp = self.epoch + b" " + self.offset
        raw_selector = raw_ref_name + b"@{" + str(selector_index).encode("ascii") + b"}"
        return ParsedReflogEntry(
            ref_name=ref_name,
            raw_ref_name=raw_ref_name,
            raw_selector=raw_selector.decode("utf-8", errors="surrogateescape"),
            raw_selector_bytes=raw_selector,
            new_id=self.new_id.decode("ascii"),
            old_id=self.old_id.decode("ascii"),
            epoch_seconds=self.epoch_seconds,
            offset_seconds=self.offset_seconds,
            raw_timestamp=raw_timestamp.decode("ascii"),
            raw_timestamp_bytes=raw_timestamp,
            actor_name=self.actor_name.decode("utf-8", errors="surrogateescape"),
            raw_actor_name=self.actor_name,
            actor_email=self.actor_email.decode("utf-8", errors="surrogateescape"),
            raw_actor_email=self.actor_email,
            raw_actor=self.identity.decode("utf-8", errors="surrogateescape"),
            raw_actor_bytes=self.identity,
            message=self.message.decode("utf-8", errors="surrogateescape"),
            raw_message=self.message,
            duplicate_ordinal=duplicate_ordinal,
        )


def parse_current_refs(payload: bytes) -> tuple[str, ...]:
    """Parse ``show-ref --head`` output and return unique current ref names."""

    refs: list[str] = []
    seen: set[str] = set()
    for line in payload.splitlines():
        oid_raw, separator, ref_raw = line.partition(b" ")
        if not separator or OID_RE.fullmatch(oid_raw) is None or not ref_raw:
            raise GitReflogParseError("invalid_git_ref_list", "show-ref returned a malformed ref")
        if b"\0" in ref_raw:
            raise GitReflogParseError("invalid_git_ref_list", "show-ref returned a NUL in a ref")
        ref_name = ref_raw.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    if "HEAD" not in seen:
        refs.insert(0, "HEAD")
    return tuple(refs)


def parse_reflog_list(payload: bytes) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in payload.splitlines():
        if not raw_ref or b"\0" in raw_ref:
            raise GitReflogParseError("invalid_git_reflog_list", "git reflog list returned an invalid ref")
        ref_name = raw_ref.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    return tuple(refs)


def parse_reflog_selectors(payload: bytes) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw_selector in payload.splitlines():
        raw_ref, separator, ordinal = raw_selector.rpartition(b"@{")
        if not separator or not raw_ref or not ordinal.endswith(b"}") or b"\0" in raw_ref:
            raise GitReflogParseError(
                "invalid_git_reflog_list",
                "git reflog fallback returned an invalid selector",
            )
        ref_name = raw_ref.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    return tuple(refs)


def parse_reflog_line(raw_line: bytes, *, ref_name: str) -> ParsedReflogLine:
    header, tab, raw_message = raw_line.partition(b"\t")
    if not tab:
        header = raw_line
        raw_message = b""
    old_raw, separator, remainder = header.partition(b" ")
    if not separator:
        raise GitReflogParseError(
            "invalid_git_reflog_entry",
            "reflog record has no old object ID",
            ref_name=ref_name,
        )
    new_raw, separator, signature_raw = remainder.partition(b" ")
    if not separator or OID_RE.fullmatch(old_raw) is None or OID_RE.fullmatch(new_raw) is None:
        raise GitReflogParseError(
            "invalid_git_reflog_object_id",
            "reflog record has an invalid old or new object ID",
            ref_name=ref_name,
        )
    if len(old_raw) != len(new_raw):
        raise GitReflogParseError(
            "invalid_git_reflog_object_id",
            "reflog old and new object IDs use different hash formats",
            ref_name=ref_name,
        )
    try:
        identity_raw, epoch_raw, offset_raw = signature_raw.rsplit(b" ", 2)
    except ValueError as error:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog identity does not end in an epoch and UTC offset",
            ref_name=ref_name,
        ) from error
    if EPOCH_RE.fullmatch(epoch_raw) is None or OFFSET_RE.fullmatch(offset_raw) is None:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an invalid epoch or UTC offset",
            ref_name=ref_name,
        )
    name_raw, email_raw = _parse_identity(identity_raw, ref_name=ref_name)
    return ParsedReflogLine(
        old_id=old_raw,
        new_id=new_raw,
        identity=identity_raw,
        epoch=epoch_raw,
        offset=offset_raw,
        actor_name=name_raw,
        actor_email=email_raw,
        message=raw_message,
        epoch_seconds=_parse_epoch(epoch_raw, ref_name=ref_name),
        offset_seconds=_parse_offset(offset_raw, ref_name=ref_name),
    )


def parse_reflog_entries(payload: bytes, *, ref_name: str) -> tuple[ParsedReflogEntry, ...]:
    """Parse semantic reflog bytes and return newest-first entries."""

    if not payload:
        return ()
    if b"\0" in payload:
        raise GitReflogParseError(
            "invalid_git_reflog_entry",
            "reflog contains an impossible NUL byte",
            ref_name=ref_name,
        )
    if not payload.endswith(b"\n"):
        raise GitReflogParseError(
            "truncated_git_reflog_entry",
            "reflog ends inside a record",
            ref_name=ref_name,
        )
    raw_lines = payload[:-1].split(b"\n")
    raw_ref_name = os.fsencode(ref_name)
    parsed_oldest_first: list[ParsedReflogEntry] = []
    duplicate_counts: dict[tuple[bytes, bytes, bytes, bytes, bytes], int] = {}
    for index, raw_line in enumerate(raw_lines):
        parsed = parse_reflog_line(raw_line, ref_name=ref_name)
        duplicate_ordinal = duplicate_counts.get(parsed.duplicate_key, 0)
        duplicate_counts[parsed.duplicate_key] = duplicate_ordinal + 1
        parsed_oldest_first.append(
            parsed.to_entry(
                ref_name=ref_name,
                raw_ref_name=raw_ref_name,
                selector_index=len(raw_lines) - index - 1,
                duplicate_ordinal=duplicate_ordinal,
            )
        )
    return tuple(reversed(parsed_oldest_first))


def _parse_offset(raw_offset: bytes, *, ref_name: str) -> int:
    hours = int(raw_offset[1:3])
    minutes = int(raw_offset[3:5])
    if hours > 23 or minutes > 59:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an out-of-range UTC offset",
            ref_name=ref_name,
        )
    sign = 1 if raw_offset[:1] == b"+" else -1
    return sign * (hours * 3_600 + minutes * 60)


def _parse_epoch(raw_epoch: bytes, *, ref_name: str) -> int:
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an epoch that cannot be represented",
            ref_name=ref_name,
        ) from error
    if not MIN_LOCALIZABLE_EPOCH <= epoch <= MAX_LOCALIZABLE_EPOCH:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an epoch outside Wuf's localizable range",
            ref_name=ref_name,
        )
    return epoch


def _parse_identity(raw_identity: bytes, *, ref_name: str) -> tuple[bytes, bytes]:
    separator = raw_identity.rfind(b" <")
    if separator < 0 or not raw_identity.endswith(b">"):
        raise GitReflogParseError(
            "invalid_git_reflog_identity",
            "reflog entry has no valid name/email identity",
            ref_name=ref_name,
        )
    return raw_identity[:separator], raw_identity[separator + 2 : -1]
