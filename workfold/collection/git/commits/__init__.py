"""Git commit discovery, materialization, and accounting."""

from workfold.collection.git.commits.collector import GitCollector
from workfold.collection.git.commits.models import (
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
