"""Exact, source-preserving parsing of raw Git commit objects."""

from __future__ import annotations

import codecs
import re
from typing import Final

from workfold.collectors.git_core.object_model import GitObjectParseError, ParsedCommit
from workfold.collectors.git_core.signatures import decode_losslessly, parse_git_signature

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def parse_commit_object(object_id: str, data: bytes) -> ParsedCommit:
    """Parse exact author/committer data from one raw commit object."""

    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None:
        raise GitObjectParseError("invalid_object_id", "invalid commit object ID", object_id=object_id)

    header_bytes, separator, message = data.partition(b"\n\n")
    if not separator:
        raise GitObjectParseError(
            "invalid_commit_object",
            "commit object has no header/message boundary",
            object_id=object_id,
        )

    singleton_headers: dict[bytes, bytes] = {}
    parent_values: list[bytes] = []
    current_name: bytes | None = None
    for line in header_bytes.split(b"\n"):
        if line.startswith(b" "):
            # Continuations (notably gpgsig) are valid but irrelevant to the
            # timestamp inventory.
            if current_name is None:
                raise GitObjectParseError(
                    "invalid_commit_header",
                    "orphaned continuation line in commit header",
                    object_id=object_id,
                )
            continue
        name, field_separator, value = line.partition(b" ")
        if not field_separator or not name:
            raise GitObjectParseError(
                "invalid_commit_header",
                "malformed commit header line",
                object_id=object_id,
            )
        current_name = name
        if name == b"parent":
            parent_values.append(value)
        elif name in {b"tree", b"author", b"committer", b"encoding"}:
            if name in singleton_headers:
                raise GitObjectParseError(
                    "invalid_commit_header",
                    f"duplicate {name.decode('ascii')} header",
                    object_id=object_id,
                )
            singleton_headers[name] = value

    missing = [name for name in (b"tree", b"author", b"committer") if name not in singleton_headers]
    if missing:
        names = ", ".join(name.decode("ascii") for name in missing)
        raise GitObjectParseError(
            "invalid_commit_header",
            f"commit object is missing required header(s): {names}",
            object_id=object_id,
        )

    tree_id = _parse_object_id(singleton_headers[b"tree"], field="tree", object_id=object_id)
    parent_ids = tuple(_parse_object_id(value, field="parent", object_id=object_id) for value in parent_values)
    author = parse_git_signature(singleton_headers[b"author"], role="author", object_id=object_id)
    committer = parse_git_signature(singleton_headers[b"committer"], role="committer", object_id=object_id)
    raw_subject = message.split(b"\n", 1)[0]
    encoding_raw = singleton_headers.get(b"encoding")
    declared_encoding = decode_losslessly(encoding_raw) if encoding_raw is not None else None
    return ParsedCommit(
        object_id=object_id,
        tree_id=tree_id,
        parent_ids=parent_ids,
        author=author,
        committer=committer,
        subject=_decode_commit_text(raw_subject, declared_encoding),
        raw_subject=raw_subject,
        declared_encoding=declared_encoding,
    )


def _parse_object_id(value: bytes, *, field: str, object_id: str) -> str:
    if not _OID_RE.fullmatch(value):
        raise GitObjectParseError(
            "invalid_commit_header",
            f"invalid {field} object ID",
            object_id=object_id,
        )
    return value.decode("ascii")


def _decode_commit_text(value: bytes, declared_encoding: str | None) -> str:
    if declared_encoding is None:
        return decode_losslessly(value)
    try:
        codecs.lookup(declared_encoding)
        return value.decode(declared_encoding, errors="surrogateescape")
    except (LookupError, UnicodeError):
        return decode_losslessly(value)


__all__ = ["parse_commit_object"]
