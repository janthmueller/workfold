"""Domain records and reconciled accounting for Git file changes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, diagnostics_are_partial
from workfold.collectors.git_core.commit_models import CollectedGitCommit
from workfold.collectors.git_core.diff_tree import ParsedGitChange
from workfold.collectors.git_core.object_model import GitSignature
from workfold.collectors.git_core.repository import GitRepository
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_file_change_id


@dataclass(frozen=True, slots=True)
class CollectedGitFileChange:
    """A parsed file change paired with its commit timestamp provenance."""

    repository: GitRepository
    commit_record: CollectedGitCommit
    change: ParsedGitChange
    diff_basis: str

    def to_origin(self) -> RecordOrigin:
        """Convert the change to the renderer-neutral domain model."""

        commit = self.commit_record.commit
        old_path = os.fsdecode(self.change.raw_old_path) if self.change.raw_old_path is not None else None
        path = os.fsdecode(self.change.raw_path)
        return RecordOrigin(
            record_id=git_file_change_id(
                self.repository.root,
                commit.object_id,
                self.diff_basis,
                self.change.raw_status,
                old_path,
                path,
            ),
            source=Source.GIT,
            record_kind=RecordKind.GIT_FILE_CHANGE,
            repository_or_root=self.repository.root,
            path=Path(path),
            old_path=Path(old_path) if old_path is not None else None,
            commit_id=commit.object_id,
            diff_basis=self.diff_basis,
            change_kind=self.change.change_kind,
            description=commit.subject,
        )

    def to_observation(self, kind: TimestampKind) -> TimestampObservation:
        """Inherit one exact author/committer slot from the owning commit."""

        signature = self.signature(kind)
        return TimestampObservation.create(
            self.to_origin(),
            kind,
            signature.epoch_nanoseconds,
            signature.raw_timestamp,
            original_offset_minutes=signature.offset_seconds // 60,
            actor_name=signature.identity.name,
            actor_email=signature.identity.email,
        )

    def signature(self, kind: TimestampKind) -> GitSignature:
        """Return the exact inherited commit signature for one timestamp kind."""

        if kind is TimestampKind.GIT_AUTHOR:
            return self.commit_record.commit.author
        if kind is TimestampKind.GIT_COMMITTER:
            return self.commit_record.commit.committer
        raise ValueError("file-change records support only Git author and committer timestamps")


@dataclass(frozen=True, slots=True)
class GitFileChangeRepositoryAccounting:
    """Reconciled file-change derivation counters for one repository."""

    repository: GitRepository
    requested_commits: int
    successful_commits: int
    parse_errors: int
    subprocess_errors: int
    discovered_changes: int
    timestamp_kinds: tuple[TimestampKind, ...] = ()
    scope_matches: tuple[tuple[TimestampKind, int], ...] = ()

    def __post_init__(self) -> None:
        counters = (
            self.requested_commits,
            self.successful_commits,
            self.parse_errors,
            self.subprocess_errors,
            self.discovered_changes,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git file-change repository counters must be non-negative")
        terminal_commits = self.successful_commits + self.parse_errors + self.subprocess_errors
        if self.requested_commits != terminal_commits:
            raise ValueError("Git file-change repository commit accounting does not reconcile")
        if len(set(self.timestamp_kinds)) != len(self.timestamp_kinds) or any(
            kind not in {TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER} for kind in self.timestamp_kinds
        ):
            raise ValueError("Git file-change timestamp kinds must be unique author/committer kinds")
        if tuple(kind for kind, _ in self.scope_matches) != self.timestamp_kinds:
            raise ValueError("Git file-change scope counts must cover the requested timestamp kinds exactly once")
        if any(count < 0 or count > self.discovered_changes for _, count in self.scope_matches):
            raise ValueError("Git file-change scope matches must be within discovered changes")

    @property
    def repository_root(self) -> Path:
        return self.repository.root

    @property
    def repository_identity(self) -> str:
        return self.repository.identity

    def timestamp_value_count(self, kind: TimestampKind) -> int:
        return self.discovered_changes if kind in self.timestamp_kinds else 0

    def scope_match_count(self, kind: TimestampKind) -> int:
        return next((count for candidate, count in self.scope_matches if candidate is kind), 0)


@dataclass(frozen=True, slots=True)
class GitFileChangeCollectionResult:
    """File-change records and collection accounting."""

    changes: tuple[CollectedGitFileChange, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_commits: int
    successful_commits: int
    discovered_changes: int
    parse_errors: int
    subprocess_errors: int
    repository_accounting: tuple[GitFileChangeRepositoryAccounting, ...] = ()
    records_retained: bool = True

    def __post_init__(self) -> None:
        counters = (
            self.requested_commits,
            self.successful_commits,
            self.discovered_changes,
            self.parse_errors,
            self.subprocess_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git file-change collection counters must be non-negative")
        terminal_commits = self.successful_commits + self.parse_errors + self.subprocess_errors
        if self.requested_commits != terminal_commits:
            raise ValueError("Git file-change commit accounting does not reconcile")
        if self.records_retained and self.discovered_changes != len(self.changes):
            raise ValueError("Git file-change discovery count does not match captured records")
        if len(self.changes) > self.discovered_changes:
            raise ValueError("retained Git file changes exceed discovered records")
        if self.repository_accounting:
            aggregate = (
                sum(item.requested_commits for item in self.repository_accounting),
                sum(item.successful_commits for item in self.repository_accounting),
                sum(item.discovered_changes for item in self.repository_accounting),
                sum(item.parse_errors for item in self.repository_accounting),
                sum(item.subprocess_errors for item in self.repository_accounting),
            )
            expected = (
                self.requested_commits,
                self.successful_commits,
                self.discovered_changes,
                self.parse_errors,
                self.subprocess_errors,
            )
            if aggregate != expected:
                raise ValueError("Git file-change repository partitions do not match aggregate counters")

    @property
    def is_partial(self) -> bool:
        return diagnostics_are_partial(self.diagnostics)


__all__ = [
    "CollectedGitFileChange",
    "GitFileChangeCollectionResult",
    "GitFileChangeRepositoryAccounting",
]
