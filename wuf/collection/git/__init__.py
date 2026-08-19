"""Safe local Git repository and commit collection."""

from wuf.collection.git.commits import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitCommitRepositoryAccounting,
)
from wuf.collection.git.evidence import (
    GitEvidenceCollectionResult,
    GitEvidenceCollector,
    GitEvidenceRequest,
)
from wuf.collection.git.objects.models import RevListScanSpec
from wuf.collection.git.repository import (
    GitRepository,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
    resolve_repository,
    unique_semantic_repositories,
)
from wuf.collection.git.revisions import enumerate_commit_ids, iter_commit_ids, parse_commit_ids
from wuf.collection.git.runner import GitCommandError, GitRunner
from wuf.domain.scope import RefScope

__all__ = [
    "CollectedGitCommit",
    "GitCollectionResult",
    "GitCollector",
    "GitCommandError",
    "GitCommitRepositoryAccounting",
    "GitEvidenceCollectionResult",
    "GitEvidenceCollector",
    "GitEvidenceRequest",
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
