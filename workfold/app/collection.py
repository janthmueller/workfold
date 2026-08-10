"""Coordinate enabled collectors and merge their bounded batches."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from workfold.app.resolution import filesystem_timestamp_kinds, git_timestamp_kinds
from workfold.collectors.base import CollectorDiagnostic
from workfold.collectors.filesystem import FilesystemCollectionResult, FilesystemCollector
from workfold.collectors.git import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
)
from workfold.collectors.git_changes import (
    CollectedGitFileChange,
    GitFileChangeCollectionResult,
    GitFileChangeCollector,
    GitFileChangeRepositoryAccounting,
)
from workfold.collectors.git_reflogs import (
    CollectedGitReflog,
    GitReflogCollectionResult,
    GitReflogCollector,
)
from workfold.collectors.git_tags import CollectedGitTag, GitTagCollectionResult, GitTagCollector
from workfold.config import FilesystemEntry, RawOptions, UsageError
from workfold.coverage import Capability
from workfold.pipeline import ObservationConsumer


@dataclass(frozen=True, slots=True)
class Collection:
    """Results and accounting emitted by one application-level collection pass."""

    diagnostics: tuple[CollectorDiagnostic, ...]
    capabilities: tuple[Capability, ...]
    any_collector_succeeded: bool
    commit_result: GitCollectionResult | None = None
    file_change_result: GitFileChangeCollectionResult | None = None
    tag_result: GitTagCollectionResult | None = None
    reflog_result: GitReflogCollectionResult | None = None
    filesystem_result: FilesystemCollectionResult | None = None
    repository_resolution: GitRepositoryResolutionResult | None = None


def collect(
    options: RawOptions,
    *,
    observation_consumer: ObservationConsumer,
    git_collector: GitCollector | None,
    repository_resolver: GitRepositoryResolver | None,
    file_change_collector: GitFileChangeCollector | None,
    tag_collector: GitTagCollector | None,
    reflog_collector: GitReflogCollector | None,
    filesystem_collector: FilesystemCollector | None,
) -> Collection:
    """Run every enabled collector and retain only aggregate accounting."""

    diagnostics: list[CollectorDiagnostic] = []
    capabilities: list[Capability] = []
    any_succeeded = False
    commit_result: GitCollectionResult | None = None
    file_result: GitFileChangeCollectionResult | None = None
    tag_result: GitTagCollectionResult | None = None
    reflog_result: GitReflogCollectionResult | None = None
    filesystem_result: FilesystemCollectionResult | None = None
    repository_resolution: GitRepositoryResolutionResult | None = None

    if options.source.includes_git:
        if options.git_records.includes_commits:
            timestamp_kinds = git_timestamp_kinds(options.git_date)
            file_results: list[GitFileChangeCollectionResult] = []
            resolved_file_change_collector = file_change_collector or GitFileChangeCollector()

            def consume_file_changes(changes: tuple[CollectedGitFileChange, ...]) -> None:
                for item in changes:
                    observation_consumer(tuple(item.to_observation(kind) for kind in timestamp_kinds))

            def consume_commits(commits: tuple[CollectedGitCommit, ...]) -> None:
                if options.git_mode.includes_commit_markers:
                    for item in commits:
                        observation_consumer(tuple(item.to_observation(kind) for kind in timestamp_kinds))
                if options.git_mode.includes_file_changes:
                    file_results.append(
                        resolved_file_change_collector.collect(
                            commits,
                            change_consumer=consume_file_changes,
                            retain_changes=False,
                        )
                    )

            commit_result = (git_collector or GitCollector()).collect(
                options.paths,
                ref_scope=options.ref_scope,
                commit_consumer=consume_commits,
                retain_commits=False,
            )
            repositories = commit_result.repositories
            diagnostics.extend(commit_result.diagnostics)
            any_succeeded |= commit_result.successful_repositories > 0 or commit_result.discovered_commit_ids > 0
            if options.git_mode.includes_file_changes:
                file_result = merge_file_change_results(file_results)
                diagnostics.extend(file_result.diagnostics)
        else:
            repository_resolution = (repository_resolver or GitRepositoryResolver()).resolve(options.paths)
            repositories = repository_resolution.repositories
            diagnostics.extend(repository_resolution.diagnostics)

        if options.git_records.includes_tags:

            def consume_tags(tags: tuple[CollectedGitTag, ...]) -> None:
                for item in tags:
                    if item.tagger is not None:
                        observation_consumer((item.to_observation(),))

            tag_result = (tag_collector or GitTagCollector()).collect(
                repositories,
                tag_consumer=consume_tags,
                retain_tags=False,
            )
            diagnostics.extend(tag_result.diagnostics)
            any_succeeded |= tag_result.successful_repositories > 0 or tag_result.discovered_tags > 0
        if options.git_records.includes_reflogs:

            def consume_reflogs(entries: tuple[CollectedGitReflog, ...]) -> None:
                for item in entries:
                    observation_consumer((item.to_observation(),))

            reflog_result = (reflog_collector or GitReflogCollector()).collect(
                repositories,
                entry_consumer=consume_reflogs,
                retain_entries=False,
            )
            diagnostics.extend(reflog_result.diagnostics)
            any_succeeded |= reflog_result.successful_repositories > 0 or reflog_result.discovered_refs > 0

    if options.source.includes_filesystem:
        try:
            filesystem_result = (filesystem_collector or FilesystemCollector()).collect(
                options.paths,
                timestamp_kinds=filesystem_timestamp_kinds(options.filesystem_times),
                include_regular_files=FilesystemEntry.FILE in options.filesystem_entries,
                include_directories=FilesystemEntry.DIRECTORY in options.filesystem_entries,
                include_symlinks=FilesystemEntry.SYMLINK in options.filesystem_entries,
                respect_gitignore=options.respect_gitignore,
                include_ignored=options.include_ignored,
                exclusions=options.exclusions,
                observation_consumer=observation_consumer,
                retain_entries=False,
                retain_observations=False,
            )
        except ValueError as error:
            raise UsageError(str(error)) from error
        diagnostics.extend(filesystem_result.diagnostics)
        capabilities.extend(filesystem_result.capabilities)
        any_succeeded |= bool(filesystem_result.successful_roots)

    return Collection(
        diagnostics=tuple(diagnostics),
        capabilities=tuple(capabilities),
        any_collector_succeeded=any_succeeded,
        commit_result=commit_result,
        file_change_result=file_result,
        tag_result=tag_result,
        reflog_result=reflog_result,
        filesystem_result=filesystem_result,
        repository_resolution=repository_resolution,
    )


def merge_file_change_results(
    results: Sequence[GitFileChangeCollectionResult],
) -> GitFileChangeCollectionResult:
    """Merge bounded Git derivation batches without reconstructing records."""

    accounting_by_repository: dict[str, GitFileChangeRepositoryAccounting] = {}
    for result in results:
        for item in result.repository_accounting:
            existing = accounting_by_repository.get(item.repository.identity)
            if existing is None:
                accounting_by_repository[item.repository.identity] = item
            else:
                accounting_by_repository[item.repository.identity] = GitFileChangeRepositoryAccounting(
                    repository=existing.repository,
                    requested_commits=existing.requested_commits + item.requested_commits,
                    successful_commits=existing.successful_commits + item.successful_commits,
                    parse_errors=existing.parse_errors + item.parse_errors,
                    subprocess_errors=existing.subprocess_errors + item.subprocess_errors,
                    discovered_changes=existing.discovered_changes + item.discovered_changes,
                )
    return GitFileChangeCollectionResult(
        changes=tuple(change for result in results for change in result.changes),
        diagnostics=tuple(diagnostic for result in results for diagnostic in result.diagnostics),
        requested_commits=sum(result.requested_commits for result in results),
        successful_commits=sum(result.successful_commits for result in results),
        discovered_changes=sum(result.discovered_changes for result in results),
        parse_errors=sum(result.parse_errors for result in results),
        subprocess_errors=sum(result.subprocess_errors for result in results),
        repository_accounting=tuple(accounting_by_repository.values()),
        records_retained=all(result.records_retained for result in results),
    )
