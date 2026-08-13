"""Safe local Git repository and commit collection."""

from workfold.collection.git.commits import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitCommitRepositoryAccounting,
)
from workfold.collection.git.objects.models import RevListScanSpec
from workfold.collection.git.repository import (
    GitRepository,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
    resolve_repository,
    unique_semantic_repositories,
)
from workfold.collection.git.revisions import enumerate_commit_ids, iter_commit_ids, parse_commit_ids
from workfold.collection.git.runner import GitCommandError, GitRunner
from workfold.domain.scope import RefScope

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
    "RevListScanSpec",
    "enumerate_commit_ids",
    "iter_commit_ids",
    "parse_commit_ids",
    "resolve_repository",
    "unique_semantic_repositories",
]
