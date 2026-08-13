"""Value objects shared by exact Git revision and object parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

GitSignatureRole: TypeAlias = Literal["author", "committer"]


class GitObjectParseError(ValueError):
    """A structured failure to parse Git revision or raw-object data."""

    def __init__(self, code: str, message: str, *, object_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.object_id = object_id


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Losslessly retained identity from a Git signature header."""

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
    subject_truncated: bool = False


@dataclass(frozen=True, slots=True)
class RevListScanSpec:
    """The minimum commit fields needed for pre-normalization selection."""

    roles: tuple[GitSignatureRole, ...]

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError("a commit scan requires at least one timestamp role")
        if any(role not in {"author", "committer"} for role in self.roles):
            raise ValueError("commit scan contains an unsupported timestamp role")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("commit scan timestamp roles must be unique")

    @property
    def pretty_format(self) -> str:
        """Return an ASCII-only format whose records remain newline framed."""

        fields = ["%H"]
        for role in self.roles:
            prefix = "a" if role == "author" else "c"
            fields.append(f"%{prefix}t")
        return "%x00".join(fields) + "%x00"


@dataclass(frozen=True, slots=True)
class RevListCommitScan:
    """Validated lightweight timestamps for one reachable commit."""

    object_id: str
    roles: tuple[GitSignatureRole, ...]
    instants_utc_ns: tuple[int, ...]

    def instant_utc_ns(self, role: GitSignatureRole) -> int:
        try:
            return self.instants_utc_ns[self.roles.index(role)]
        except ValueError as error:
            raise ValueError(f"commit scan did not request the {role} timestamp") from error


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


BatchObjectResult: TypeAlias = BatchObject | UnavailableBatchObject


@dataclass(frozen=True, slots=True)
class UnexpectedBatchObject:
    """A requested object whose raw type does not match the required type."""

    object_id: str
    object_type: str


@dataclass(frozen=True, slots=True)
class InvalidBatchCommit:
    """One malformed commit returned without aborting the remaining batch."""

    object_id: str
    code: str
    message: str


CommitBatchResult: TypeAlias = ParsedCommit | UnavailableBatchObject | UnexpectedBatchObject | InvalidBatchCommit


@dataclass(frozen=True, slots=True)
class CompactBatchObject:
    """Bounded timestamp-relevant metadata retained from one raw object."""

    object_id: str
    object_type: str
    data: bytes
    subject_truncated: bool = False


@dataclass(frozen=True, slots=True)
class InvalidBatchObject:
    """One malformed raw object returned without aborting its batch."""

    object_id: str
    code: str
    message: str


CompactBatchResult: TypeAlias = CompactBatchObject | UnavailableBatchObject | UnexpectedBatchObject | InvalidBatchObject


@dataclass(frozen=True, slots=True)
class BatchParseResult:
    """All decoded batch objects and per-object unavailable statuses."""

    objects: tuple[BatchObject, ...]
    unavailable: tuple[UnavailableBatchObject, ...]


__all__ = [
    "BatchObject",
    "BatchObjectResult",
    "BatchParseResult",
    "CompactBatchObject",
    "CompactBatchResult",
    "CommitBatchResult",
    "GitIdentity",
    "GitObjectParseError",
    "GitSignature",
    "GitSignatureRole",
    "InvalidBatchCommit",
    "InvalidBatchObject",
    "ParsedCommit",
    "RevListCommitScan",
    "RevListScanSpec",
    "UnavailableBatchObject",
    "UnexpectedBatchObject",
]
