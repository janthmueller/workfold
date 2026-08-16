"""Build renderer-neutral scope and operational facts for reports."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from workfold.application.collection import Collection
from workfold.application.report import (
    CollectionFacts,
    DiagnosticFacts,
    GitCommitInputFacts,
    GitCommitInputTargetFacts,
    GitFileChangeFacts,
    GitFileChangeTargetFacts,
    GitReflogFacts,
    GitTagFacts,
    ReportContext,
    ReportScope,
)
from workfold.application.resolution import ResolvedTimeSelection
from workfold.collection.diagnostics import DiagnosticKind, DiagnosticSeverity
from workfold.collection.git.evidence import GitCommitInputSummary, GitFileChangeSummary
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import CoverageLedger
from workfold.domain.schedule import Schedule


def build_report_context(
    *,
    options: RunOptions,
    collection: Collection,
    time_selection: ResolvedTimeSelection,
    timezone: ZoneInfo,
    schedule: Schedule,
    coverage: CoverageLedger,
) -> ReportContext:
    """Project execution state into the stable report boundary."""

    return ReportContext(
        scope=ReportScope(
            period_label=time_selection.label,
            timezone_name=timezone.key,
            schedule=schedule,
            profile_name=options.profile.value,
            evidence=options.evidence,
            ref_scope=options.ref_scope,
            git_identities=options.git_identities,
            include_ignored=options.include_ignored,
            exclusions=options.exclusions,
        ),
        collection=_collection_facts(collection),
        coverage=coverage,
    )


def _collection_facts(collection: Collection) -> CollectionFacts:
    errors, warnings, infos = collection.diagnostic_counts
    diagnostic_facts = DiagnosticFacts(
        errors=errors,
        warnings=warnings,
        infos=infos,
        filesystem_inventory_failures=sum(
            item.completeness_failure_count
            for item in collection.diagnostics
            if item.kind is DiagnosticKind.FILESYSTEM_INVENTORY
        ),
        filesystem_inventory_errors=sum(
            item.occurrence_count(DiagnosticSeverity.ERROR)
            for item in collection.diagnostics
            if item.kind is DiagnosticKind.FILESYSTEM_INVENTORY
        ),
        other_completeness_failures=sum(
            max(0, item.completeness_failure_count - item.occurrence_count(DiagnosticSeverity.ERROR))
            for item in collection.diagnostics
            if item.kind is not DiagnosticKind.FILESYSTEM_INVENTORY
        ),
    )
    git = collection.git_summary
    filesystem = collection.filesystem_summary
    return CollectionFacts(
        diagnostics=diagnostic_facts,
        capabilities=collection.capabilities,
        git_roots=tuple(os.fspath(item) for item in git.roots) if git is not None else (),
        filesystem_roots=(tuple(os.fspath(item) for item in filesystem.roots) if filesystem is not None else ()),
        pruned_ignored_subtrees=(filesystem.pruned_ignored_subtrees if filesystem is not None else 0),
        commit_inputs=(
            _commit_input_facts(git.commit_inputs) if git is not None and git.commit_inputs is not None else None
        ),
        file_changes=(
            _file_change_facts(git.file_changes) if git is not None and git.file_changes is not None else None
        ),
        duplicate_commit_ids=git.duplicate_commit_ids if git is not None else 0,
        duplicate_git_targets=git.duplicate_targets if git is not None else 0,
        linked_worktree_contexts=git.linked_worktree_contexts if git is not None else 0,
        tags=(
            GitTagFacts(git.tags.annotated, git.tags.lightweight) if git is not None and git.tags is not None else None
        ),
        reflogs=(
            GitReflogFacts(git.reflogs.available, git.reflogs.unavailable)
            if git is not None and git.reflogs is not None
            else None
        ),
        overlapping_filesystem_roots=(filesystem.overlapping_roots_deduplicated if filesystem is not None else 0),
    )


def _commit_input_facts(summary: GitCommitInputSummary) -> GitCommitInputFacts:
    return GitCommitInputFacts(
        reachable=summary.reachable,
        examined=summary.examined,
        candidates=summary.candidates,
        hydrated=summary.hydrated,
        selected=summary.selected,
        scope_evaluation_errors=summary.scope_evaluation_errors,
        record_errors=summary.record_errors,
        targets=tuple(
            GitCommitInputTargetFacts(
                root=os.fspath(item.root),
                reachable=item.reachable,
                examined=item.examined,
                candidates=item.candidates,
                hydrated=item.hydrated,
                selected=item.selected,
                scope_evaluation_errors=item.scope_evaluation_errors,
                unavailable=item.unavailable,
                parse_failures=item.parse_failures,
                operational_errors=item.operational_errors,
            )
            for item in summary.targets
        ),
    )


def _file_change_facts(summary: GitFileChangeSummary) -> GitFileChangeFacts:
    return GitFileChangeFacts(
        commits_requested=summary.commits_requested,
        successfully_parsed=summary.successfully_parsed,
        parse_failures=summary.parse_failures,
        subprocess_failures=summary.subprocess_failures,
        changes_discovered=summary.changes_discovered,
        targets=tuple(
            GitFileChangeTargetFacts(
                root=os.fspath(item.root),
                commits_requested=item.commits_requested,
                successfully_parsed=item.successfully_parsed,
                parse_failures=item.parse_failures,
                subprocess_failures=item.subprocess_failures,
                changes_discovered=item.changes_discovered,
            )
            for item in summary.targets
        ),
    )


__all__ = ["build_report_context"]
