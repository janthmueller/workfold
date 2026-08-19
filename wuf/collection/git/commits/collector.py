"""Coordinate bounded commit collection across selected Git repositories."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from wuf.collection.diagnostics import DiagnosticBuffer
from wuf.collection.git.commits.models import (
    CollectedGitCommit,
    CommitScopeFilter,
    GitCollectionResult,
    GitCommitRepositoryAccounting,
    MaterializedCommitScopeFilter,
)
from wuf.collection.git.commits.repository import collect_repository_commits
from wuf.collection.git.objects.models import RevListScanSpec
from wuf.collection.git.repository import (
    GitRepositoryResolver,
    group_semantic_repositories,
)
from wuf.collection.git.runner import GitRunner
from wuf.domain.scope import RefScope

_DEFAULT_OBJECT_BATCH_BYTES = 8 * 1_024 * 1_024


class GitCollector:
    """Collect unique raw commit records from one or more selected paths."""

    def __init__(
        self,
        runner: GitRunner | None = None,
        *,
        object_batch_size: int = 2_048,
        object_batch_bytes: int = _DEFAULT_OBJECT_BATCH_BYTES,
    ) -> None:
        if object_batch_size < 1:
            raise ValueError("object_batch_size must be positive")
        if object_batch_bytes < 1:
            raise ValueError("object_batch_bytes must be positive")
        self._runner = runner or GitRunner()
        self._object_batch_size = object_batch_size
        self._object_batch_bytes = object_batch_bytes

    def collect(
        self,
        paths: Sequence[Path],
        *,
        ref_scope: RefScope = RefScope.ALL_REFS,
        commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None = None,
        scan_spec: RevListScanSpec | None = None,
        commit_filter: CommitScopeFilter | None = None,
        materialized_filter: MaterializedCommitScopeFilter | None = None,
        retain_commits: bool = True,
    ) -> GitCollectionResult:
        if (scan_spec is None) != (commit_filter is None):
            raise ValueError("commit scan spec and filter must be supplied together")
        if materialized_filter is not None and commit_filter is None:
            raise ValueError("materialized commit filter requires a commit scan filter")
        if commit_filter is not None and retain_commits:
            raise ValueError("filtered commit collection cannot retain a complete record inventory")
        resolution = GitRepositoryResolver(self._runner).resolve(paths)
        diagnostics = DiagnosticBuffer()
        diagnostics.extend(resolution.diagnostics)
        repositories = list(resolution.repositories)
        commits: list[CollectedGitCommit] = []
        repository_accounting: list[GitCommitRepositoryAccounting] = []

        for repository_contexts in group_semantic_repositories(repositories):
            repository_accounting.append(
                collect_repository_commits(
                    repository_contexts[0],
                    repository_contexts=repository_contexts,
                    runner=self._runner,
                    ref_scope=ref_scope,
                    object_batch_size=self._object_batch_size,
                    object_batch_bytes=self._object_batch_bytes,
                    diagnostics=diagnostics,
                    commits=commits,
                    commit_consumer=commit_consumer,
                    scan_spec=scan_spec,
                    commit_filter=commit_filter,
                    materialized_filter=materialized_filter,
                    retain_commits=retain_commits,
                )
            )

        return GitCollectionResult(
            repositories=tuple(repositories),
            commits=tuple(commits),
            diagnostics=diagnostics.snapshot(),
            requested_targets=len(paths),
            successful_repositories=sum(item.successful for item in repository_accounting),
            discovered_commit_ids=sum(item.discovered_commit_ids for item in repository_accounting),
            duplicate_commit_ids=sum(item.duplicate_commit_ids for item in repository_accounting),
            unavailable_objects=sum(item.unavailable_objects for item in repository_accounting),
            parse_errors=sum(item.parse_errors for item in repository_accounting),
            repository_accounting=tuple(repository_accounting),
            duplicate_targets=resolution.duplicate_targets,
            records_retained=retain_commits,
        )


__all__ = ["GitCollector"]
