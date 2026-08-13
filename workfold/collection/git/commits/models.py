"""Domain records and reconciled accounting for Git commit collection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from workfold.collection.diagnostics import CollectorDiagnostic, diagnostics_are_partial
from workfold.collection.git.objects.models import (
    GitSignatureRole,
    ParsedCommit,
    RevListCommitScan,
)
from workfold.collection.git.repository import GitRepository
from workfold.domain.observations import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.domain.provenance import git_commit_id

CommitScopeDecision = tuple[GitSignatureRole, ...]
CommitScopeFilter = Callable[[GitRepository, RevListCommitScan], CommitScopeDecision | bool]
MaterializedCommitScopeFilter = Callable[
    [GitRepository, ParsedCommit, CommitScopeDecision],
    CommitScopeDecision | bool,
]


@dataclass(frozen=True, slots=True)
class CollectedGitCommit:
    """A raw parsed commit paired with its containing repository."""

    repository: GitRepository
    commit: ParsedCommit

    def to_origin(self) -> RecordOrigin:
        return RecordOrigin(
            record_id=git_commit_id(self.repository.root, self.commit.object_id),
            source=Source.GIT,
            record_kind=RecordKind.COMMIT,
            repository_or_root=self.repository.root,
            commit_id=self.commit.object_id,
            description=self.commit.subject,
        )

    def to_observation(self, kind: TimestampKind) -> TimestampObservation:
        if kind is TimestampKind.GIT_AUTHOR:
            signature = self.commit.author
        elif kind is TimestampKind.GIT_COMMITTER:
            signature = self.commit.committer
        else:
            raise ValueError("commit records support only Git author and committer timestamps")
        return TimestampObservation.create(
            self.to_origin(),
            kind,
            signature.epoch_nanoseconds,
            signature.raw_timestamp,
            original_offset_minutes=signature.offset_seconds // 60,
            actor_name=signature.identity.name,
            actor_email=signature.identity.email,
        )


@dataclass(frozen=True, slots=True)
class GitCommitRepositoryAccounting:
    """Reconciled commit collection counters for one resolved repository."""

    repository: GitRepository
    discovered_commit_ids: int
    examined_commits: int
    candidate_commits: int
    selected_commits: int
    hydrated_commits: int
    duplicate_commit_ids: int
    unavailable_objects: int
    parse_errors: int
    operational_errors: int
    successful: bool
    timestamp_roles: tuple[GitSignatureRole, ...]
    timestamp_values_read: tuple[tuple[GitSignatureRole, int], ...] = ()
    scope_matches: tuple[tuple[GitSignatureRole, int], ...] = ()
    materialized_scope_matches: tuple[tuple[GitSignatureRole, int], ...] = ()
    scope_evaluation_errors: tuple[tuple[GitSignatureRole, int], ...] = ()

    def __post_init__(self) -> None:
        counters = (
            self.discovered_commit_ids,
            self.examined_commits,
            self.candidate_commits,
            self.selected_commits,
            self.hydrated_commits,
            self.duplicate_commit_ids,
            self.unavailable_objects,
            self.parse_errors,
            self.operational_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git commit repository counters must be non-negative")
        if self.examined_commits > self.discovered_commit_ids:
            raise ValueError("examined Git commits exceed reachable commits")
        if self.candidate_commits > self.examined_commits:
            raise ValueError("candidate Git commits exceed examined commits")
        if self.hydrated_commits > self.candidate_commits:
            raise ValueError("hydrated Git commits exceed candidates")
        if self.selected_commits > self.hydrated_commits:
            raise ValueError("selected Git commits exceed hydrated commits")
        _validate_roles(self.timestamp_roles)
        for label, counts in (
            ("timestamp values read", self.timestamp_values_read),
            ("scope matches", self.scope_matches),
            ("materialized scope matches", self.materialized_scope_matches),
            ("scope evaluation errors", self.scope_evaluation_errors),
        ):
            _validate_role_counts(counts, roles=self.timestamp_roles, label=label)
        for role in self.timestamp_roles:
            values_read = self.timestamp_value_count(role)
            matches = self.scope_match_count(role)
            materialized = self.materialized_scope_match_count(role)
            scope_errors = self.scope_evaluation_error_count(role)
            if values_read != self.examined_commits:
                raise ValueError(f"Git {role} timestamp values must equal examined commits")
            if matches > values_read:
                raise ValueError(f"Git {role} scope matches exceed timestamp values read")
            if matches + scope_errors > values_read:
                raise ValueError(f"Git {role} scope outcomes exceed timestamp values read")
            if materialized > matches:
                raise ValueError(f"materialized Git {role} scope matches exceed matches")
            if materialized > self.selected_commits:
                raise ValueError(f"materialized Git {role} scope matches exceed selected commits")
            if materialized > self.hydrated_commits:
                raise ValueError(f"materialized Git {role} scope matches exceed hydrated commits")
        if self.selected_commits > sum(count for _, count in self.materialized_scope_matches):
            raise ValueError("selected Git commits exceed materialized timestamp roles")

    @property
    def repository_root(self) -> Path:
        return self.repository.root

    @property
    def repository_identity(self) -> str:
        return self.repository.identity

    @property
    def eligible_commits(self) -> int:
        """Return records whose requested evidence was usable for coverage."""

        return self.discovered_commit_ids - self.record_errors

    @property
    def scope_errors(self) -> int:
        """Return records whose timestamp scope could not be evaluated."""

        return self.discovered_commit_ids - self.examined_commits

    @property
    def hydration_errors(self) -> int:
        """Return candidate records whose rich provenance could not be read."""

        return self.candidate_commits - self.hydrated_commits

    @property
    def record_errors(self) -> int:
        """Return all records that could hide a requested observation."""

        return self.scope_errors + self.hydration_errors

    def timestamp_value_count(self, role: GitSignatureRole) -> int:
        return _role_count(self.timestamp_values_read, role)

    def scope_match_count(self, role: GitSignatureRole) -> int:
        return _role_count(self.scope_matches, role)

    def materialized_scope_match_count(self, role: GitSignatureRole) -> int:
        return _role_count(self.materialized_scope_matches, role)

    def materialization_error_count(self, role: GitSignatureRole) -> int:
        return self.scope_match_count(role) - self.materialized_scope_match_count(role)

    def scope_evaluation_error_count(self, role: GitSignatureRole) -> int:
        return _role_count(self.scope_evaluation_errors, role)


@dataclass(frozen=True, slots=True)
class GitCollectionResult:
    """Quick-view Git collection plus accounting needed by the shared ledger."""

    repositories: tuple[GitRepository, ...]
    commits: tuple[CollectedGitCommit, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_targets: int
    successful_repositories: int
    discovered_commit_ids: int
    duplicate_commit_ids: int
    unavailable_objects: int
    parse_errors: int
    repository_accounting: tuple[GitCommitRepositoryAccounting, ...] = ()
    duplicate_targets: int = 0
    records_retained: bool = True

    def __post_init__(self) -> None:
        selected = sum(item.selected_commits for item in self.repository_accounting)
        if self.repository_accounting:
            if self.records_retained and len(self.commits) != selected:
                raise ValueError("retained Git commits do not match repository accounting")
            if len(self.commits) > selected:
                raise ValueError("retained Git commits exceed selected repository accounting")

    @property
    def is_partial(self) -> bool:
        return diagnostics_are_partial(self.diagnostics)


def _validate_roles(roles: tuple[GitSignatureRole, ...]) -> None:
    if not roles or len(set(roles)) != len(roles) or any(role not in {"author", "committer"} for role in roles):
        raise ValueError("Git timestamp roles must contain unique author/committer roles")


def _validate_role_counts(
    counts: tuple[tuple[GitSignatureRole, int], ...],
    *,
    roles: tuple[GitSignatureRole, ...],
    label: str,
) -> None:
    count_roles = tuple(role for role, _ in counts)
    if count_roles != roles:
        raise ValueError(f"Git {label} must cover the requested timestamp roles exactly once")
    if any(count < 0 for _, count in counts):
        raise ValueError(f"Git {label} must be non-negative")


def _role_count(counts: tuple[tuple[GitSignatureRole, int], ...], role: GitSignatureRole) -> int:
    return next((count for candidate, count in counts if candidate == role), 0)


__all__ = [
    "CollectedGitCommit",
    "CommitScopeDecision",
    "CommitScopeFilter",
    "GitCollectionResult",
    "GitCommitRepositoryAccounting",
    "MaterializedCommitScopeFilter",
]
