"""Fuse exact Git observation scope into commit discovery."""

from __future__ import annotations

from workfold.collectors.git import GitRepository
from workfold.collectors.git_objects import (
    GitSignatureRole,
    RevListCommitScan,
    RevListScanSpec,
)
from workfold.models import Source, TimestampKind
from workfold.scope import ObservationScope


class GitCommitSelection:
    """Apply the shared observation scope before full commit hydration.

    Git cannot prune exact author-date queries, so every reachable commit still
    passes through the lightweight scan. Only records with a selected timestamp
    role are hydrated into full commit objects.
    """

    def __init__(
        self,
        scope: ObservationScope,
        timestamp_kinds: tuple[TimestampKind, ...],
    ) -> None:
        self._scope = scope
        self._timestamp_kinds = timestamp_kinds
        roles: tuple[GitSignatureRole, ...] = tuple(_timestamp_role(kind) for kind in timestamp_kinds)
        self.scan_spec = RevListScanSpec(
            roles,
            include_identities=scope.filters_git_identities,
        )

    def select(self, _repository: GitRepository, scanned: RevListCommitScan) -> tuple[GitSignatureRole, ...]:
        """Return matching roles that require full commit hydration."""

        return tuple(
            _timestamp_role(kind) for kind in self._timestamp_kinds if self._includes_scanned_timestamp(scanned, kind)
        )

    def _includes_scanned_timestamp(
        self,
        scanned: RevListCommitScan,
        kind: TimestampKind,
    ) -> bool:
        role = _timestamp_role(kind)
        actor_name: str | None = None
        actor_email: str | None = None
        if self._scope.filters_git_identities:
            actor_name, actor_email = scanned.identity(role)
        return self._scope.includes_timestamp(
            instant_utc_ns=scanned.instant_utc_ns(role),
            source=Source.GIT,
            actor_name=actor_name,
            actor_email=actor_email,
        )


def should_preselect_commits(
    scope: ObservationScope | None,
) -> bool:
    """Return whether an exact lightweight scope scan avoids rich work."""

    return bool(scope is not None and scope.is_restrictive_for(Source.GIT))


def _timestamp_role(kind: TimestampKind) -> GitSignatureRole:
    if kind is TimestampKind.GIT_AUTHOR:
        return "author"
    if kind is TimestampKind.GIT_COMMITTER:
        return "committer"
    raise ValueError(f"commit collector does not support {kind.value}")


__all__ = ["GitCommitSelection", "should_preselect_commits"]
