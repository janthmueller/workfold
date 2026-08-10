"""Semantic reflog value objects and structured failures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, DiagnosticSeverity
from workfold.collectors.git import GitRepository
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_reflog_id


class GitReflogParseError(ValueError):
    """A structured failure to parse reflog discovery or semantic records."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        ref_name: str | None = None,
        record_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.ref_name = ref_name
        self.record_count = record_count


class GitReflogReadError(OSError):
    """A structured failure to resolve or safely read one semantic reflog."""

    def __init__(self, code: str, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class ReflogRef:
    """Availability and extraction counts for one repository reflog."""

    repository: GitRepository
    ref_name: str
    entry_count: int
    captured_entry_count: int

    @property
    def unavailable_entry_count(self) -> int:
        """Return inventoried records whose timestamp could not be captured."""

        return max(0, self.entry_count - self.captured_entry_count)


@dataclass(frozen=True, slots=True)
class ParsedReflogEntry:
    """One exact timestamp-bearing record from a semantic reflog."""

    ref_name: str
    raw_ref_name: bytes
    raw_selector: str
    raw_selector_bytes: bytes
    new_id: str
    old_id: str
    epoch_seconds: int
    offset_seconds: int
    raw_timestamp: str
    raw_timestamp_bytes: bytes
    actor_name: str
    raw_actor_name: bytes
    actor_email: str
    raw_actor_email: bytes
    raw_actor: str
    raw_actor_bytes: bytes
    message: str
    raw_message: bytes
    duplicate_ordinal: int

    @property
    def epoch_nanoseconds(self) -> int:
        return self.epoch_seconds * 1_000_000_000


@dataclass(frozen=True, slots=True)
class ReflogVisit:
    """Counts and snapshot state from one bounded semantic reflog visit."""

    entry_count: int
    captured_entry_count: int
    changed_during_read: bool


@dataclass(frozen=True, slots=True)
class CollectedGitReflog:
    """A semantic reflog entry paired with its repository."""

    repository: GitRepository
    entry: ParsedReflogEntry

    def to_origin(self) -> RecordOrigin:
        """Convert the raw entry to a provenance-preserving domain record."""

        return RecordOrigin(
            record_id=git_reflog_id(
                self.repository.root,
                self.entry.ref_name,
                self.entry.old_id,
                self.entry.new_id,
                self.entry.raw_selector,
                self.entry.raw_timestamp,
                self.entry.raw_actor,
                self.entry.message,
                self.entry.duplicate_ordinal,
            ),
            source=Source.GIT,
            record_kind=RecordKind.REFLOG,
            repository_or_root=self.repository.root,
            object_id=self.entry.new_id,
            target_id=self.entry.old_id,
            ref_name=self.entry.ref_name,
            description=self.entry.message,
        )

    def to_observation(self) -> TimestampObservation:
        """Convert the exact reflog timestamp to a normalized observation."""

        return TimestampObservation.create(
            self.to_origin(),
            TimestampKind.GIT_REFLOG,
            self.entry.epoch_nanoseconds,
            self.entry.raw_timestamp,
            original_offset_minutes=self.entry.offset_seconds // 60,
            actor_name=self.entry.actor_name,
            actor_email=self.entry.actor_email,
        )


@dataclass(frozen=True, slots=True)
class GitReflogCollectionResult:
    """Reflog records plus per-ref availability accounting."""

    entries: tuple[CollectedGitReflog, ...]
    available_refs: tuple[ReflogRef, ...]
    refs_without_reflog: tuple[ReflogRef, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_repositories: int
    successful_repositories: int
    discovered_refs: int
    captured_entries: int
    unavailable_entries: int
    parse_errors: int
    records_retained: bool = True

    def __post_init__(self) -> None:
        if self.records_retained and len(self.entries) != self.captured_entries:
            raise ValueError("retained reflog entries do not match captured entry accounting")
        if len(self.entries) > self.captured_entries:
            raise ValueError("retained reflog entries exceed captured entry accounting")

    @property
    def is_partial(self) -> bool:
        return any(item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics)
