"""Parse machine-safe Git tag refs and annotated tag objects."""

from __future__ import annotations

import re
from typing import Final

from wuf.collection.git.objects.models import GitObjectParseError
from wuf.collection.git.objects.signatures import parse_git_signature
from wuf.collection.git.tags.models import DiscoveredGitTag, ParsedTagObject

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitTagParseError(ValueError):
    """A structured failure to parse tag discovery or a raw tag object."""

    def __init__(self, code: str, message: str, *, object_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.object_id = object_id


def parse_tag_refs(payload: bytes) -> tuple[DiscoveredGitTag, ...]:
    """Parse NUL-delimited fields from ``for-each-ref`` tag output."""

    if not payload:
        return ()
    records: list[DiscoveredGitTag] = []
    for raw_line in payload.splitlines():
        fields = raw_line.split(b"\0")
        if len(fields) != 4 or fields[-1] != b"":
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned malformed tag fields")
        ref_raw, object_raw, type_raw, _terminator = fields
        if not ref_raw.startswith(b"refs/tags/") or len(ref_raw) == len(b"refs/tags/"):
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag ref")
        if _OID_RE.fullmatch(object_raw) is None:
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag object ID")
        if type_raw not in {b"blob", b"commit", b"tag", b"tree"}:
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag object type")
        records.append(
            DiscoveredGitTag(
                ref_name=ref_raw.decode("utf-8", errors="surrogateescape"),
                raw_ref_name=ref_raw,
                object_id=object_raw.decode("ascii"),
                object_type=type_raw.decode("ascii"),
            )
        )
    return tuple(records)


def parse_tag_object(object_id: str, data: bytes) -> ParsedTagObject:
    """Parse exact target, optional tagger signature and subject from a tag object."""

    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None:
        raise GitTagParseError("invalid_tag_object_id", "invalid tag object ID", object_id=object_id)
    header_bytes, separator, message = data.partition(b"\n\n")
    if not separator:
        raise GitTagParseError(
            "invalid_tag_object",
            "tag object has no header/message boundary",
            object_id=object_id,
        )
    headers: dict[bytes, bytes] = {}
    current_name: bytes | None = None
    for line in header_bytes.split(b"\n"):
        if line.startswith(b" "):
            if current_name is None:
                raise GitTagParseError(
                    "invalid_tag_header",
                    "orphaned continuation line in tag header",
                    object_id=object_id,
                )
            continue
        name, field_separator, value = line.partition(b" ")
        if not field_separator or not name:
            raise GitTagParseError(
                "invalid_tag_header",
                "malformed tag header line",
                object_id=object_id,
            )
        current_name = name
        if name in {b"object", b"type", b"tag", b"tagger"}:
            if name in headers:
                raise GitTagParseError(
                    "invalid_tag_header",
                    f"duplicate {name.decode('ascii')} header",
                    object_id=object_id,
                )
            headers[name] = value

    missing = [name for name in (b"object", b"type", b"tag") if name not in headers]
    if missing:
        names = ", ".join(name.decode("ascii") for name in missing)
        raise GitTagParseError(
            "invalid_tag_header",
            f"tag object is missing required header(s): {names}",
            object_id=object_id,
        )
    target_raw = headers[b"object"]
    if _OID_RE.fullmatch(target_raw) is None:
        raise GitTagParseError(
            "invalid_tag_header",
            "tag object has an invalid target object ID",
            object_id=object_id,
        )
    type_raw = headers[b"type"]
    if type_raw not in {b"blob", b"commit", b"tag", b"tree"}:
        raise GitTagParseError(
            "invalid_tag_header",
            "tag object has an invalid target object type",
            object_id=object_id,
        )
    tagger_raw = headers.get(b"tagger")
    try:
        tagger = parse_git_signature(tagger_raw, role="tagger", object_id=object_id) if tagger_raw is not None else None
    except GitObjectParseError as error:
        raise GitTagParseError(error.code, str(error), object_id=object_id) from error
    raw_subject = message.split(b"\n", 1)[0]
    return ParsedTagObject(
        object_id=object_id,
        target_id=target_raw.decode("ascii"),
        target_type=type_raw.decode("ascii"),
        tag_name=headers[b"tag"].decode("utf-8", errors="surrogateescape"),
        raw_tag_name=headers[b"tag"],
        tagger=tagger,
        subject=raw_subject.decode("utf-8", errors="surrogateescape"),
        raw_subject=raw_subject,
    )
