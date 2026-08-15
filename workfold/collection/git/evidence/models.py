"""Source-level requests, outcomes, and summaries for local Git evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from workfold.collection.diagnostics import CollectorDiagnostic
from workfold.domain.coverage import CoverageFragment
from workfold.domain.observations import TimestampKind, TimestampObservation
from workfold.domain.scope import ObservationScope, RefScope

GitObservationConsumer = Callable[[tuple[TimestampObservation, ...]], None]
_COMMIT_TIMESTAMP_KINDS = frozenset({TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER})


@dataclass(frozen=True, slots=True)
class GitEvidenceRequest:
    """Exact Git record and timestamp partitions requested by the application."""

    paths: tuple[Path, ...]
    ref_scope: RefScope
    commit_timestamps: tuple[TimestampKind, ...] = ()
    file_change_timestamps: tuple[TimestampKind, ...] = ()
    collect_tags: bool = False
    collect_reflogs: bool = False
    observation_scope: ObservationScope | None = None

    def __post_init__(self) -> None:
        if not self.paths:
            raise ValueError("Git evidence collection requires at least one path")
        _validate_commit_timestamps(self.commit_timestamps, label="commit")
        _validate_commit_timestamps(self.file_change_timestamps, label="file-change")
        if not self.has_work:
            raise ValueError("Git evidence collection requires at least one evidence kind")

    @property
    def commit_scan_timestamps(self) -> tuple[TimestampKind, ...]:
        """Return commit roles needed directly or for file-change derivation."""

        requested = {*self.commit_timestamps, *self.file_change_timestamps}
        return tuple(kind for kind in (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER) if kind in requested)

    @property
    def has_work(self) -> bool:
        return bool(
            self.commit_timestamps
            or self.file_change_timestamps
            or self.collect_tags
            or self.collect_reflogs
        )


@dataclass(frozen=True, slots=True)
class GitCommitInputTargetSummary:
    root: Path
    reachable: int
    examined: int
    candidates: int
    hydrated: int
    selected: int
    scope_evaluation_errors: int
    unavailable: int
    parse_failures: int
    operational_errors: int


@dataclass(frozen=True, slots=True)
class GitCommitInputSummary:
    reachable: int
    examined: int
    candidates: int
    hydrated: int
    selected: int
    scope_evaluation_errors: int
    record_errors: int
    targets: tuple[GitCommitInputTargetSummary, ...]


@dataclass(frozen=True, slots=True)
class GitFileChangeTargetSummary:
    root: Path
    commits_requested: int
    successfully_parsed: int
    parse_failures: int
    subprocess_failures: int
    changes_discovered: int


@dataclass(frozen=True, slots=True)
class GitFileChangeSummary:
    commits_requested: int
    successfully_parsed: int
    parse_failures: int
    subprocess_failures: int
    changes_discovered: int
    targets: tuple[GitFileChangeTargetSummary, ...]


@dataclass(frozen=True, slots=True)
class GitTagSummary:
    annotated: int
    lightweight: int


@dataclass(frozen=True, slots=True)
class GitReflogSummary:
    available: int
    unavailable: int


@dataclass(frozen=True, slots=True)
class GitEvidenceSummary:
    """Stable source-level facts independent of individual Git collectors."""

    roots: tuple[Path, ...] = ()
    commit_inputs: GitCommitInputSummary | None = None
    file_changes: GitFileChangeSummary | None = None
    duplicate_commit_ids: int = 0
    duplicate_targets: int = 0
    linked_worktree_contexts: int = 0
    tags: GitTagSummary | None = None
    reflogs: GitReflogSummary | None = None


@dataclass(frozen=True, slots=True)
class GitEvidenceCollectionResult:
    """One coherent Git source outcome with its collector-owned accounting."""

    diagnostics: tuple[CollectorDiagnostic, ...]
    successful: bool
    coverage: CoverageFragment
    summary: GitEvidenceSummary


def _validate_commit_timestamps(kinds: tuple[TimestampKind, ...], *, label: str) -> None:
    if len(set(kinds)) != len(kinds) or any(kind not in _COMMIT_TIMESTAMP_KINDS for kind in kinds):
        raise ValueError(f"Git {label} timestamps must be unique author/committer kinds")


__all__ = [
    "GitCommitInputSummary",
    "GitCommitInputTargetSummary",
    "GitEvidenceCollectionResult",
    "GitEvidenceRequest",
    "GitEvidenceSummary",
    "GitFileChangeSummary",
    "GitFileChangeTargetSummary",
    "GitObservationConsumer",
    "GitReflogSummary",
    "GitTagSummary",
]
