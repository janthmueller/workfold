"""Project detailed Git collector results into stable source summaries."""

from __future__ import annotations

from workfold.collection.git.changes import GitFileChangeCollectionResult
from workfold.collection.git.commits import GitCollectionResult
from workfold.collection.git.evidence.models import (
    GitCommitInputSummary,
    GitCommitInputTargetSummary,
    GitEvidenceRequest,
    GitEvidenceSummary,
    GitFileChangeSummary,
    GitFileChangeTargetSummary,
    GitReflogSummary,
    GitTagSummary,
)
from workfold.collection.git.reflogs import GitReflogCollectionResult
from workfold.collection.git.repository import GitRepositoryResolutionResult
from workfold.collection.git.tags import GitTagCollectionResult


def build_git_evidence_summary(
    *,
    request: GitEvidenceRequest,
    commit_result: GitCollectionResult | None,
    file_change_result: GitFileChangeCollectionResult | None,
    tag_result: GitTagCollectionResult | None,
    reflog_result: GitReflogCollectionResult | None,
    repository_resolution: GitRepositoryResolutionResult | None,
) -> GitEvidenceSummary:
    """Summarize one source pass without retaining subcollector results."""

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


__all__ = ["build_git_evidence_summary"]
