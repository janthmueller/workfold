"""Orchestrate local Git tag discovery and bounded object extraction."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.git import GitCommandError, GitRepository, GitRunner, unique_semantic_repositories
from workfold.collectors.tags.models import (
    CollectedGitTag,
    DiscoveredGitTag,
    GitTagCollectionResult,
    GitTagRepositoryAccounting,
)
from workfold.collectors.tags.parser import GitTagParseError, parse_tag_refs
from workfold.collectors.tags.reader import collect_tag_batch, command_diagnostic
from workfold.iterables import batched


class GitTagCollector:
    """Collect local lightweight and annotated tags without traversing commits."""

    def __init__(self, runner: GitRunner | None = None, *, ref_batch_size: int = 512) -> None:
        if ref_batch_size < 1:
            raise ValueError("ref_batch_size must be positive")
        self._runner = runner or GitRunner()
        self._ref_batch_size = ref_batch_size

    def collect(
        self,
        repositories: Sequence[GitRepository],
        *,
        tag_consumer: Callable[[tuple[CollectedGitTag, ...]], None] | None = None,
        retain_tags: bool = True,
    ) -> GitTagCollectionResult:
        """Collect all local tag refs independently of commit reachability scope."""

        semantic_repositories = unique_semantic_repositories(repositories)
        tags: list[CollectedGitTag] = []
        diagnostics = DiagnosticBuffer()
        repository_accounting: list[GitTagRepositoryAccounting] = []

        for repository in semantic_repositories:
            error_count_start = diagnostics.error_count
            discovered_for_repository = 0
            captured_tags_for_repository = 0
            annotated_for_repository = 0
            lightweight_for_repository = 0
            captured_timestamps_for_repository = 0
            unavailable_timestamps_for_repository = 0
            unavailable_objects_for_repository = 0
            parse_errors_for_repository = 0
            successful_for_repository = False
            try:
                repository_failed = False
                try:
                    raw_lines = self._runner.iter_stdout_lines(
                        (
                            "for-each-ref",
                            "--format=%(refname)%00%(objectname)%00%(objecttype)%00",
                            "refs/tags/",
                        ),
                        cwd=repository.root,
                    )
                    for raw_batch in batched(raw_lines, self._ref_batch_size):
                        refs: list[DiscoveredGitTag] = []
                        for raw_line in raw_batch:
                            try:
                                refs.extend(parse_tag_refs(raw_line))
                            except GitTagParseError as error:
                                parse_errors_for_repository += 1
                                repository_failed = True
                                diagnostics.append(
                                    CollectorDiagnostic(
                                        code=error.code,
                                        stage="git_tag_discovery",
                                        target=os.fspath(repository.root),
                                        message=str(error),
                                    )
                                )
                        if not refs:
                            continue
                        ref_batch = tuple(refs)
                        discovered_for_repository += len(ref_batch)
                        annotated_for_repository += sum(item.annotated for item in ref_batch)
                        lightweight_for_repository += sum(not item.annotated for item in ref_batch)
                        outcome = collect_tag_batch(repository, ref_batch, self._runner, diagnostics)
                        captured_tags_for_repository += outcome.captured_tags
                        captured_timestamps_for_repository += outcome.captured_timestamps
                        unavailable_timestamps_for_repository += outcome.unavailable_timestamps
                        unavailable_objects_for_repository += outcome.unavailable_objects
                        parse_errors_for_repository += outcome.parse_errors
                        repository_failed |= outcome.failed
                        if retain_tags:
                            tags.extend(outcome.collected)
                        if outcome.collected and tag_consumer is not None:
                            tag_consumer(outcome.collected)
                except GitCommandError as error:
                    diagnostics.append(command_diagnostic(error, repository=repository, stage="git_tag_discovery"))
                    repository_failed = True
                successful_for_repository = not repository_failed
            finally:
                operational_errors = diagnostics.error_count - error_count_start
                repository_accounting.append(
                    GitTagRepositoryAccounting(
                        repository=repository,
                        discovered_tags=discovered_for_repository,
                        captured_tags=captured_tags_for_repository,
                        record_errors=discovered_for_repository - captured_tags_for_repository,
                        annotated_tags=annotated_for_repository,
                        lightweight_tags=lightweight_for_repository,
                        captured_tagger_timestamps=captured_timestamps_for_repository,
                        unavailable_tagger_timestamps=unavailable_timestamps_for_repository,
                        unavailable_objects=unavailable_objects_for_repository,
                        parse_errors=parse_errors_for_repository,
                        operational_errors=operational_errors,
                        successful=successful_for_repository,
                    )
                )

        successful = sum(item.successful for item in repository_accounting)
        discovered = sum(item.discovered_tags for item in repository_accounting)
        annotated = sum(item.annotated_tags for item in repository_accounting)
        lightweight = sum(item.lightweight_tags for item in repository_accounting)
        captured = sum(item.captured_tagger_timestamps for item in repository_accounting)
        unavailable_tagger = sum(item.unavailable_tagger_timestamps for item in repository_accounting)
        unavailable_objects = sum(item.unavailable_objects for item in repository_accounting)
        parse_errors = sum(item.parse_errors for item in repository_accounting)

        return GitTagCollectionResult(
            tags=tuple(tags),
            diagnostics=diagnostics.snapshot(),
            requested_repositories=len(semantic_repositories),
            successful_repositories=successful,
            discovered_tags=discovered,
            annotated_tags=annotated,
            lightweight_tags=lightweight,
            captured_tagger_timestamps=captured,
            unavailable_tagger_timestamps=unavailable_tagger,
            unavailable_objects=unavailable_objects,
            parse_errors=parse_errors,
            repository_accounting=tuple(repository_accounting),
            records_retained=retain_tags,
        )
