"""Local timestamp collectors."""

from workfold.collectors.base import CollectorDiagnostic, CollectorResult, DiagnosticSeverity
from workfold.collectors.filesystem import (
    CollectedFilesystemEntry,
    FilesystemAccounting,
    FilesystemCollectionResult,
    FilesystemCollector,
    TimestampExtractionCoverage,
    scandir_no_follow,
)
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter, TimestampExtraction
from workfold.collectors.git import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitCommandError,
    GitCommitRepositoryAccounting,
    GitRepository,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
    GitRunner,
    RefScope,
    unique_semantic_repositories,
)
from workfold.collectors.git_changes import GitFileChangeRepositoryAccounting
from workfold.collectors.git_tags import GitTagRepositoryAccounting
from workfold.collectors.ignores import GitFilesystemInventory

__all__ = [
    "CollectedGitCommit",
    "CollectedFilesystemEntry",
    "CollectorDiagnostic",
    "CollectorResult",
    "DiagnosticSeverity",
    "FilesystemAccounting",
    "FilesystemCollectionResult",
    "FilesystemCollector",
    "FilesystemTimestampAdapter",
    "GitCommitRepositoryAccounting",
    "GitCollectionResult",
    "GitCollector",
    "GitCommandError",
    "GitFileChangeRepositoryAccounting",
    "GitFilesystemInventory",
    "GitRepository",
    "GitRepositoryResolutionResult",
    "GitRepositoryResolver",
    "GitRunner",
    "GitTagRepositoryAccounting",
    "RefScope",
    "TimestampExtraction",
    "TimestampExtractionCoverage",
    "scandir_no_follow",
    "unique_semantic_repositories",
]
