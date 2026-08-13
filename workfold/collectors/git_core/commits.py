"""Compatibility façade for Git commit collection and accounting."""

from workfold.collectors.git_core.commit_collector import GitCollector
from workfold.collectors.git_core.commit_models import (
    CollectedGitCommit,
    CommitScopeDecision,
    CommitScopeFilter,
    GitCollectionResult,
    GitCommitRepositoryAccounting,
    MaterializedCommitScopeFilter,
)

__all__ = [
    "CollectedGitCommit",
    "CommitScopeDecision",
    "CommitScopeFilter",
    "GitCollectionResult",
    "GitCollector",
    "GitCommitRepositoryAccounting",
    "MaterializedCommitScopeFilter",
]
