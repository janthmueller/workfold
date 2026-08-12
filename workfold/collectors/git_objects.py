"""Machine-safe parsing for raw objects returned by ``git cat-file``."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MIN_LOCALIZABLE_EPOCH_SECONDS: Final[int] = -62_135_510_400
_MAX_LOCALIZABLE_EPOCH_SECONDS: Final[int] = 253_402_214_399
GitSignatureRole: TypeAlias = Literal["author", "committer"]


class GitObjectParseError(ValueError):
    """A structured failure to parse a Git batch envelope or raw object."""

    def __init__(self, code: str, message: str, *, object_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.object_id = object_id


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Losslessly retained identity from an author/committer header."""

    name: str
    email: str
    raw: bytes
    raw_name: bytes
    raw_email: bytes


@dataclass(frozen=True, slots=True)
class GitSignature:
    """One Git identity and its exact stored timestamp representation."""

    identity: GitIdentity
    epoch_seconds: int
    offset_seconds: int
    raw: bytes
    raw_timestamp: str
    raw_timestamp_bytes: bytes
    raw_offset: str

    @property
    def epoch_nanoseconds(self) -> int:
        """Return the normalized UTC instant at nanosecond precision."""

        return self.epoch_seconds * 1_000_000_000


@dataclass(frozen=True, slots=True)
class ParsedCommit:
    """Relevant provenance extracted directly from a raw commit object."""

    object_id: str
    tree_id: str
    parent_ids: tuple[str, ...]
    author: GitSignature
    committer: GitSignature
    subject: str
    raw_subject: bytes
    declared_encoding: str | None


@dataclass(frozen=True, slots=True)
class RevListScanSpec:
    """The minimum commit fields needed for pre-normalization selection."""

    roles: tuple[GitSignatureRole, ...]
    include_identities: bool = False

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError("a commit scan requires at least one timestamp role")
        if any(role not in {"author", "committer"} for role in self.roles):
            raise ValueError("commit scan contains an unsupported timestamp role")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("commit scan timestamp roles must be unique")

    @property
    def pretty_format(self) -> str:
        """Return a NUL-field format whose records remain newline framed."""

        fields = ["%H"]
        for role in self.roles:
            prefix = "a" if role == "author" else "c"
            if self.include_identities:
                fields.extend((f"%{prefix}n", f"%{prefix}e"))
            fields.append(f"%{prefix}t")
        return "%x00".join(fields) + "%x00"


@dataclass(frozen=True, slots=True)
class RevListCommitScan:
    """Validated lightweight timestamps for one reachable commit."""

    object_id: str
    roles: tuple[GitSignatureRole, ...]
    instants_utc_ns: tuple[int, ...]
    identities_raw: tuple[tuple[bytes, bytes] | None, ...]

    def instant_utc_ns(self, role: GitSignatureRole) -> int:
        try:
            return self.instants_utc_ns[self.roles.index(role)]
        except ValueError as error:
            raise ValueError(f"commit scan did not request the {role} timestamp") from error

    def identity(self, role: GitSignatureRole) -> tuple[str, str]:
        try:
            raw = self.identities_raw[self.roles.index(role)]
        except ValueError as error:
            raise ValueError(f"commit scan did not request the {role} timestamp") from error
        if raw is None:
            raise ValueError("commit scan did not request identities")
        return _decode_losslessly(raw[0]), _decode_losslessly(raw[1])


@dataclass(frozen=True, slots=True)
class BatchObject:
    """One complete object from the ``cat-file --batch`` protocol."""

    object_id: str
    object_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class UnavailableBatchObject:
    """An object for which Git returned a batch-level status."""

    requested_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class BatchParseResult:
    """All decoded batch objects and per-object unavailable statuses."""

    objects: tuple[BatchObject, ...]
    unavailable: tuple[UnavailableBatchObject, ...]


def _decode_losslessly(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _decode_commit_text(value: bytes, declared_encoding: str | None) -> str:
    if declared_encoding is None:
        return _decode_losslessly(value)
    try:
        codecs.lookup(declared_encoding)
        return value.decode(declared_encoding, errors="surrogateescape")
    except (LookupError, UnicodeError):
        # The raw bytes and declared value remain preserved even when a
        # malformed/unknown encoding cannot be honored.
        return _decode_losslessly(value)


def _parse_object_id(value: bytes, *, field: str, object_id: str) -> str:
    if not _OID_RE.fullmatch(value):
        raise GitObjectParseError(
            "invalid_commit_header",
            f"invalid {field} object ID",
            object_id=object_id,
        )
    return value.decode("ascii")


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
    epoch_seconds = _parse_git_epoch(epoch_raw, role=role, object_id=object_id)
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
    offset_seconds = sign * (offset_hours * 3_600 + offset_minutes * 60)
    return epoch_seconds, offset_seconds


def _parse_git_epoch(epoch_raw: bytes, *, role: str, object_id: str) -> int:
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
            f"{role} header epoch is outside Workfold's localizable datetime range",
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
        name=_decode_losslessly(raw_name),
        email=_decode_losslessly(raw_email),
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


def _formatted_signature(name: bytes, email: bytes, timestamp: bytes) -> bytes:
    return name + b" <" + email + b"> " + timestamp


def parse_commit_object(object_id: str, data: bytes) -> ParsedCommit:
    """Parse exact author/committer data from one raw commit object.

    Signatures are parsed from the right because names can contain spaces.  The
    normalized epoch is deliberately not inferred from formatted ``git log``
    output, and the original epoch/offset bytes remain available as provenance.
    """

    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id):
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
            # Continuations (notably gpgsig) are not relevant to the timestamp
            # inventory, but accepting them is required for valid signed commits.
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
    declared_encoding = _decode_losslessly(encoding_raw) if encoding_raw is not None else None
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


def inspect_rev_list_scan(record: bytes, spec: RevListScanSpec) -> RevListCommitScan:
    """Parse one minimal commit-selection record emitted for ``spec``."""

    fields = _parse_rev_list_fields(record)
    expected_fields = 1 + len(spec.roles) * (3 if spec.include_identities else 1)
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

    cursor = 1
    instants: list[int] = []
    identities: list[tuple[bytes, bytes] | None] = []
    for role in spec.roles:
        identity: tuple[bytes, bytes] | None = None
        if spec.include_identities:
            identity = (fields[cursor], fields[cursor + 1])
            cursor += 2
        epoch_seconds = _parse_git_epoch(fields[cursor], role=role, object_id=object_id)
        cursor += 1
        instants.append(epoch_seconds * 1_000_000_000)
        identities.append(identity)
    return RevListCommitScan(object_id, spec.roles, tuple(instants), tuple(identities))


def parse_rev_list_metadata(record: bytes) -> ParsedCommit:
    """Parse one fixed-field, newline-framed ``git rev-list`` record."""

    fields = _parse_rev_list_fields(record)
    if len(fields) != 11:
        raise GitObjectParseError(
            "invalid_rev_list_record",
            f"rev-list metadata record has {len(fields)} fields instead of 11",
        )
    object_id_raw = fields[0]
    if not _OID_RE.fullmatch(object_id_raw):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list metadata record has no valid commit object ID",
        )
    object_id = object_id_raw.decode("ascii")
    tree_id = _parse_object_id(fields[1], field="tree", object_id=object_id)
    parent_ids = tuple(_parse_object_id(parent, field="parent", object_id=object_id) for parent in fields[2].split())
    author = parse_git_signature(
        _formatted_signature(fields[3], fields[4], fields[5]),
        role="author",
        object_id=object_id,
    )
    committer = parse_git_signature(
        _formatted_signature(fields[6], fields[7], fields[8]),
        role="committer",
        object_id=object_id,
    )
    declared_encoding = _decode_losslessly(fields[9]) if fields[9] else None
    return ParsedCommit(
        object_id=object_id,
        tree_id=tree_id,
        parent_ids=parent_ids,
        author=author,
        committer=committer,
        subject=_decode_commit_text(fields[10], declared_encoding),
        raw_subject=fields[10],
        declared_encoding=declared_encoding,
    )


def _parse_rev_list_fields(record: bytes) -> list[bytes]:
    if not record.endswith(b"\n"):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list metadata record has no newline terminator",
        )
    payload = record[:-1]
    if not payload.endswith(b"\0"):
        raise GitObjectParseError(
            "invalid_rev_list_record",
            "rev-list metadata record has no final field terminator",
        )
    return payload[:-1].split(b"\0")


def parse_cat_file_batch(payload: bytes, expected_object_ids: tuple[str, ...]) -> BatchParseResult:
    """Parse ``git cat-file --batch`` output using its byte-counted protocol."""

    cursor = 0
    objects: list[BatchObject] = []
    unavailable: list[UnavailableBatchObject] = []

    for expected_id in expected_object_ids:
        line_end = payload.find(b"\n", cursor)
        if line_end < 0:
            raise GitObjectParseError(
                "truncated_cat_file_batch",
                "cat-file batch output ended before an object header",
                object_id=expected_id,
            )
        header = payload[cursor:line_end]
        cursor = line_end + 1
        fields = header.split(b" ")

        if len(fields) == 2:
            requested_raw, reason_raw = fields
            if _decode_losslessly(requested_raw) != expected_id:
                raise GitObjectParseError(
                    "unexpected_cat_file_object",
                    "cat-file batch response order does not match its request",
                    object_id=expected_id,
                )
            unavailable.append(
                UnavailableBatchObject(
                    requested_id=expected_id,
                    reason=_decode_losslessly(reason_raw),
                )
            )
            continue

        if len(fields) != 3:
            raise GitObjectParseError(
                "invalid_cat_file_header",
                "malformed cat-file batch object header",
                object_id=expected_id,
            )
        returned_raw, object_type_raw, size_raw = fields
        if not _OID_RE.fullmatch(returned_raw) or returned_raw.decode("ascii") != expected_id:
            raise GitObjectParseError(
                "unexpected_cat_file_object",
                "cat-file returned a different object than requested",
                object_id=expected_id,
            )
        try:
            size = int(size_raw)
        except ValueError as error:
            raise GitObjectParseError(
                "invalid_cat_file_header",
                "cat-file returned a non-integer object size",
                object_id=expected_id,
            ) from error
        if size < 0:
            raise GitObjectParseError(
                "invalid_cat_file_header",
                "cat-file returned a negative object size",
                object_id=expected_id,
            )
        object_end = cursor + size
        if object_end > len(payload):
            raise GitObjectParseError(
                "truncated_cat_file_batch",
                "cat-file batch output ended inside an object",
                object_id=expected_id,
            )
        data = payload[cursor:object_end]
        cursor = object_end
        if payload[cursor : cursor + 1] != b"\n":
            raise GitObjectParseError(
                "invalid_cat_file_terminator",
                "cat-file object was not followed by its protocol newline",
                object_id=expected_id,
            )
        cursor += 1
        objects.append(
            BatchObject(
                object_id=expected_id,
                object_type=_decode_losslessly(object_type_raw),
                data=data,
            )
        )

    if cursor != len(payload):
        raise GitObjectParseError(
            "unexpected_cat_file_output",
            "cat-file batch returned trailing unrequested output",
        )
    return BatchParseResult(objects=tuple(objects), unavailable=tuple(unavailable))


__all__ = [
    "BatchObject",
    "BatchParseResult",
    "GitIdentity",
    "GitObjectParseError",
    "GitSignatureRole",
    "GitSignature",
    "ParsedCommit",
    "RevListCommitScan",
    "RevListScanSpec",
    "UnavailableBatchObject",
    "inspect_rev_list_scan",
    "parse_cat_file_batch",
    "parse_commit_object",
    "parse_git_signature",
    "parse_rev_list_metadata",
]
