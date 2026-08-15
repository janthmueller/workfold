"""Apply exact shared observation scope during Git commit discovery."""

from __future__ import annotations

from workfold.collection.git.objects.models import (
    GitSignatureRole,
    ParsedCommit,
    RevListCommitScan,
    RevListScanSpec,
)
from workfold.collection.git.repository import GitRepository
from workfold.domain.observations import Source, TimestampKind
from workfold.domain.scope import ObservationScope


class GitCommitSelection:
    """Apply the shared scope around exact raw commit hydration.

    Git cannot prune exact author-date queries, so every reachable commit still
    passes through an ASCII-only timestamp scan. Only date-range candidates are
    hydrated; identity matching then uses the raw author/committer headers.
    """

    def __init__(
        self,
        scope: ObservationScope,
        timestamp_kinds: tuple[TimestampKind, ...],
    ) -> None:
        self._scope = scope
        roles: tuple[GitSignatureRole, ...] = tuple(_timestamp_role(kind) for kind in timestamp_kinds)
        self.scan_spec = RevListScanSpec(roles)

    @property
    def requires_materialized_selection(self) -> bool:
        """Return whether raw identities are required to finish selection."""

        return self._scope.filters_git_identities

    def select_candidates(
        self,
        _repository: GitRepository,
        scanned: RevListCommitScan,
    ) -> tuple[GitSignatureRole, ...]:
        """Return date-matching roles that require raw commit hydration."""

        return tuple(
            role for role in self.scan_spec.roles if self._scope.selected_range.contains(scanned.instant_utc_ns(role))
        )

    def select_materialized(
        self,
        _repository: GitRepository,
        commit: ParsedCommit,
        candidate_roles: tuple[GitSignatureRole, ...],
    ) -> tuple[GitSignatureRole, ...]:
        """Return exact scope matches using source-preserving signatures."""

        return tuple(role for role in candidate_roles if self._includes_signature(commit, role))

    def _includes_signature(self, commit: ParsedCommit, role: GitSignatureRole) -> bool:
        signature = commit.author if role == "author" else commit.committer
        return self._scope.includes_timestamp(
            instant_utc_ns=signature.epoch_nanoseconds,
            source=Source.GIT,
            actor_name=signature.identity.name,
            actor_email=signature.identity.email,
        )


def _timestamp_role(kind: TimestampKind) -> GitSignatureRole:
    if kind is TimestampKind.GIT_AUTHOR:
        return "author"
    if kind is TimestampKind.GIT_COMMITTER:
        return "committer"
    raise ValueError(f"commit collector does not support {kind.value}")


__all__ = ["GitCommitSelection"]
