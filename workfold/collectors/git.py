"""Safe local Git repository and commit collection facade."""

from workfold.collectors.git_core.commits import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitCommitRepositoryAccounting,
    enumerate_commit_ids,
    iter_commit_ids,
    parse_commit_ids,
)
from workfold.collectors.git_core.repository import (
    GitRepository,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
    resolve_repository,
    unique_semantic_repositories,
)
from workfold.collectors.git_core.runner import GitCommandError, GitRunner
from workfold.config import RefScope

__all__ = [
    "CollectedGitCommit",
    "GitCollectionResult",
    "GitCollector",
    "GitCommandError",
    "GitCommitRepositoryAccounting",
    "GitRepository",
    "GitRepositoryResolutionResult",
    "GitRepositoryResolver",
    "GitRunner",
    "RefScope",
    "enumerate_commit_ids",
    "iter_commit_ids",
    "parse_commit_ids",
    "resolve_repository",
    "unique_semantic_repositories",
]
