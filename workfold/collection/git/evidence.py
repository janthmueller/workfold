"""Source-level coordination for all supported local Git evidence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from workfold.collection.diagnostics import CollectorDiagnostic
from workfold.collection.git.changes import (
    CollectedGitFileChange,
    GitFileChangeCollectionResult,
    GitFileChangeCollector,
    GitFileChangeRepositoryAccounting,
)
from workfold.collection.git.commits import CollectedGitCommit, GitCollectionResult, GitCollector
from workfold.collection.git.coverage import build_git_coverage_fragment
from workfold.collection.git.reflogs import CollectedGitReflog, GitReflogCollectionResult, GitReflogCollector
from workfold.collection.git.repository import GitRepositoryResolutionResult, GitRepositoryResolver
from workfold.collection.git.selection import GitCommitSelection
from workfold.collection.git.tags import CollectedGitTag, GitTagCollectionResult, GitTagCollector
from workfold.domain.coverage import CoverageFragment
from workfold.domain.observations import Source, TimestampKind, TimestampObservation
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


@dataclass(frozen=True, slots=True)
class GitEvidenceCollector:
    """Coordinate Git adapters behind one source-level application boundary."""

    commits: GitCollector = field(default_factory=GitCollector)
    repositories: GitRepositoryResolver = field(default_factory=GitRepositoryResolver)
    file_changes: GitFileChangeCollector = field(default_factory=GitFileChangeCollector)
    tags: GitTagCollector = field(default_factory=GitTagCollector)
    reflogs: GitReflogCollector = field(default_factory=GitReflogCollector)

    def collect(
        self,
        request: GitEvidenceRequest,
        *,
        observation_consumer: GitObservationConsumer,
    ) -> GitEvidenceCollectionResult:
        """Collect every requested Git evidence partition and emit selected observations."""

        diagnostics: list[CollectorDiagnostic] = []
        successful = False
        commit_result: GitCollectionResult | None = None
        file_result: GitFileChangeCollectionResult | None = None
        tag_result: GitTagCollectionResult | None = None
        reflog_result: GitReflogCollectionResult | None = None
        repository_resolution: GitRepositoryResolutionResult | None = None

        def emit(observations: Sequence[TimestampObservation]) -> None:
            if not observations:
                return
            selected = (
                tuple(observations)
                if request.observation_scope is None
                or not request.observation_scope.is_restrictive_for(Source.GIT)
                else request.observation_scope.select(observations)
            )
            if selected:
                observation_consumer(selected)

        if request.commit_scan_timestamps:
            file_results: list[GitFileChangeCollectionResult] = []
            commit_selection = (
                GitCommitSelection(request.observation_scope, request.commit_scan_timestamps)
                if request.observation_scope is not None
                else None
            )

            def consume_file_changes(changes: tuple[CollectedGitFileChange, ...]) -> None:
                for item in changes:
                    emit(tuple(item.to_observation(kind) for kind in request.file_change_timestamps))

            def consume_commits(commits: tuple[CollectedGitCommit, ...]) -> None:
                if request.commit_timestamps:
                    for item in commits:
                        emit(tuple(item.to_observation(kind) for kind in request.commit_timestamps))
                if request.file_change_timestamps:
                    change_commits = tuple(
                        item
                        for item in commits
                        if _commit_matches_scope(
                            item,
                            request.file_change_timestamps,
                            request.observation_scope,
                        )
                    )
                    if not change_commits:
                        return
                    file_results.append(
                        self.file_changes.collect(
                            change_commits,
                            change_consumer=consume_file_changes,
                            timestamp_kinds=request.file_change_timestamps,
                            observation_scope=request.observation_scope,
                            retain_changes=False,
                        )
                    )

            commit_result = self.commits.collect(
                request.paths,
                ref_scope=request.ref_scope,
                commit_consumer=consume_commits,
                scan_spec=commit_selection.scan_spec if commit_selection is not None else None,
                commit_filter=commit_selection.select_candidates if commit_selection is not None else None,
                materialized_filter=(
                    commit_selection.select_materialized
                    if commit_selection is not None and commit_selection.requires_materialized_selection
                    else None
                ),
                retain_commits=False,
            )
            repositories = commit_result.repositories
            diagnostics.extend(commit_result.diagnostics)
            successful |= commit_result.successful_repositories > 0 or commit_result.discovered_commit_ids > 0
            if request.file_change_timestamps:
                file_result = merge_file_change_results(file_results)
                diagnostics.extend(file_result.diagnostics)
        else:
            repository_resolution = self.repositories.resolve(request.paths)
            repositories = repository_resolution.repositories
            diagnostics.extend(repository_resolution.diagnostics)

        if request.collect_tags:

            def consume_tags(tags: tuple[CollectedGitTag, ...]) -> None:
                for item in tags:
                    if item.tagger is not None:
                        emit((item.to_observation(),))

            tag_result = self.tags.collect(
                repositories,
                tag_consumer=consume_tags,
                observation_scope=request.observation_scope,
                retain_tags=False,
            )
            diagnostics.extend(tag_result.diagnostics)
            successful |= tag_result.successful_repositories > 0 or tag_result.discovered_tags > 0

        if request.collect_reflogs:

            def consume_reflogs(entries: tuple[CollectedGitReflog, ...]) -> None:
                for item in entries:
                    emit((item.to_observation(),))

            reflog_result = self.reflogs.collect(
                repositories,
                entry_consumer=consume_reflogs,
                observation_scope=request.observation_scope,
                retain_entries=False,
            )
            diagnostics.extend(reflog_result.diagnostics)
            successful |= reflog_result.successful_repositories > 0 or reflog_result.discovered_refs > 0

        coverage = build_git_coverage_fragment(
            commit_result=commit_result,
            file_change_result=file_result,
            tag_result=tag_result,
            reflog_result=reflog_result,
            commit_timestamps=request.commit_timestamps,
            file_change_timestamps=request.file_change_timestamps,
        )
        summary = _build_summary(
            request=request,
            commit_result=commit_result,
            file_change_result=file_result,
            tag_result=tag_result,
            reflog_result=reflog_result,
            repository_resolution=repository_resolution,
        )
        return GitEvidenceCollectionResult(
            diagnostics=tuple(diagnostics),
            successful=successful,
            coverage=coverage,
            summary=summary,
        )


def _build_summary(
    *,
    request: GitEvidenceRequest,
    commit_result: GitCollectionResult | None,
    file_change_result: GitFileChangeCollectionResult | None,
    tag_result: GitTagCollectionResult | None,
    reflog_result: GitReflogCollectionResult | None,
    repository_resolution: GitRepositoryResolutionResult | None,
) -> GitEvidenceSummary:
    repositories = (
        commit_result.repositories
        if commit_result is not None
        else (repository_resolution.repositories if repository_resolution is not None else ())
    )
    roots = tuple(dict.fromkeys(item.root for item in repositories))
    duplicate_targets = 0
    linked_worktree_contexts = 0
    if commit_result is not None:
        duplicate_targets += commit_result.duplicate_targets
        if commit_result.repository_accounting:
            linked_worktree_contexts = max(
                0,
                len(commit_result.repositories) - len(commit_result.repository_accounting),
            )
    if repository_resolution is not None:
        duplicate_targets += repository_resolution.duplicate_targets
    return GitEvidenceSummary(
        roots=roots,
        commit_inputs=(
            _commit_input_summary(commit_result)
            if request.file_change_timestamps and commit_result is not None
            else None
        ),
        file_changes=(
            _file_change_summary(file_change_result) if file_change_result is not None else None
        ),
        duplicate_commit_ids=commit_result.duplicate_commit_ids if commit_result is not None else 0,
        duplicate_targets=duplicate_targets,
        linked_worktree_contexts=linked_worktree_contexts,
        tags=(
            GitTagSummary(tag_result.annotated_tags, tag_result.lightweight_tags)
            if tag_result is not None
            else None
        ),
        reflogs=(
            GitReflogSummary(len(reflog_result.available_refs), len(reflog_result.refs_without_reflog))
            if reflog_result is not None
            else None
        ),
    )


def _commit_input_summary(result: GitCollectionResult) -> GitCommitInputSummary:
    accounting = result.repository_accounting
    examined = sum(item.examined_commits for item in accounting)
    candidates = sum(item.candidate_commits for item in accounting)
    selected = sum(item.selected_commits for item in accounting)
    hydrated = sum(item.hydrated_commits for item in accounting)
    if not accounting:
        examined = candidates = selected = hydrated = len(result.commits)
    return GitCommitInputSummary(
        reachable=result.discovered_commit_ids,
        examined=examined,
        candidates=candidates,
        hydrated=hydrated,
        selected=selected,
        scope_evaluation_errors=sum(
            count for item in accounting for _role, count in item.scope_evaluation_errors
        ),
        record_errors=sum(item.record_errors for item in accounting),
        targets=tuple(
            GitCommitInputTargetSummary(
                root=item.repository.root,
                reachable=item.discovered_commit_ids,
                examined=item.examined_commits,
                candidates=item.candidate_commits,
                hydrated=item.hydrated_commits,
                selected=item.selected_commits,
                scope_evaluation_errors=sum(count for _role, count in item.scope_evaluation_errors),
                unavailable=item.unavailable_objects,
                parse_failures=item.parse_errors,
                operational_errors=item.operational_errors,
            )
            for item in accounting
        ),
    )


def _file_change_summary(result: GitFileChangeCollectionResult) -> GitFileChangeSummary:
    return GitFileChangeSummary(
        commits_requested=result.requested_commits,
        successfully_parsed=result.successful_commits,
        parse_failures=result.parse_errors,
        subprocess_failures=result.subprocess_errors,
        changes_discovered=result.discovered_changes,
        targets=tuple(
            GitFileChangeTargetSummary(
                root=item.repository.root,
                commits_requested=item.requested_commits,
                successfully_parsed=item.successful_commits,
                parse_failures=item.parse_errors,
                subprocess_failures=item.subprocess_errors,
                changes_discovered=item.discovered_changes,
            )
            for item in result.repository_accounting
        ),
    )


def _commit_matches_scope(
    item: CollectedGitCommit,
    timestamp_kinds: tuple[TimestampKind, ...],
    scope: ObservationScope | None,
) -> bool:
    """Return whether a hydrated commit can produce a requested scoped role."""

    if scope is None:
        return True
    for kind in timestamp_kinds:
        observation = item.to_observation(kind)
        if scope.includes_timestamp(
            instant_utc_ns=observation.instant_utc_ns,
            source=Source.GIT,
            actor_name=observation.actor_name,
            actor_email=observation.actor_email,
        ):
            return True
    return False


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
                if existing.timestamp_kinds != item.timestamp_kinds:
                    raise ValueError("cannot merge file-change accounting with different timestamp kinds")
                accounting_by_repository[item.repository.identity] = GitFileChangeRepositoryAccounting(
                    repository=existing.repository,
                    requested_commits=existing.requested_commits + item.requested_commits,
                    successful_commits=existing.successful_commits + item.successful_commits,
                    parse_errors=existing.parse_errors + item.parse_errors,
                    subprocess_errors=existing.subprocess_errors + item.subprocess_errors,
                    discovered_changes=existing.discovered_changes + item.discovered_changes,
                    timestamp_kinds=existing.timestamp_kinds,
                    scope_matches=tuple(
                        (
                            kind,
                            existing.scope_match_count(kind) + item.scope_match_count(kind),
                        )
                        for kind in existing.timestamp_kinds
                    ),
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


def _validate_commit_timestamps(kinds: tuple[TimestampKind, ...], *, label: str) -> None:
    if len(set(kinds)) != len(kinds) or any(kind not in _COMMIT_TIMESTAMP_KINDS for kind in kinds):
        raise ValueError(f"Git {label} timestamps must be unique author/committer kinds")


__all__ = [
    "GitCommitInputSummary",
    "GitCommitInputTargetSummary",
    "GitEvidenceCollectionResult",
    "GitEvidenceCollector",
    "GitEvidenceRequest",
    "GitEvidenceSummary",
    "GitFileChangeSummary",
    "GitFileChangeTargetSummary",
    "GitObservationConsumer",
    "GitReflogSummary",
    "GitTagSummary",
    "merge_file_change_results",
]
