"""Verbose terminal coverage reconciliation."""

from __future__ import annotations

from collections import Counter

from workfold.application.collection import Collection
from workfold.application.collection_plan import CollectionPlan
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import CapabilityStatus, CoverageLedger
from workfold.domain.evidence import EvidenceKind
from workfold.domain.observations import RecordKind
from workfold.reporting.terminal.coverage_scope import (
    coverage_scope_details,
    pruned_ignored_subtree_label,
    record_label,
)


def coverage_details(
    ledger: CoverageLedger,
    collection: Collection,
    options: RunOptions,
) -> tuple[str, ...]:
    """Render verbose coverage accounting for terminal presentation."""

    details: list[str] = [
        f"timestamp slots examined: {ledger.slots_examined:,}",
        f"timestamp values read: {ledger.timestamp_values_read:,}",
        f"timestamp values matching scope: {ledger.timestamp_values_matching_scope:,}",
        f"timestamp observations selected: {ledger.observations_selected:,}",
        f"activity markers plotted: {ledger.markers_plotted:,}",
        *coverage_scope_details(options),
    ]
    coalesced_total = sum(item.coalesced_into_markers for item in ledger.timestamps)
    if coalesced_total:
        details.append(f"coalesced for plotting with roles preserved: {coalesced_total:,}")
    record_counts: dict[RecordKind, Counter[str]] = {}
    for item in ledger.records:
        counts = record_counts.setdefault(item.key.record_kind, Counter())
        counts.update(
            discovered=item.discovered,
            eligible=item.eligible,
            ignored=item.ignored,
            explicitly_excluded=item.explicitly_excluded,
            excluded_entry_type=item.excluded_entry_type,
            semantic_git_admin=item.semantic_git_admin,
            record_errors=item.record_errors,
        )
    for kind in sorted(record_counts, key=lambda item: item.value):
        counts = record_counts[kind]
        line = f"{record_label(kind)} discovered: {counts['discovered']:,}"
        outcomes = (
            ("eligible", "eligible"),
            ("ignored", "ignored"),
            ("explicitly_excluded", "explicitly excluded"),
            ("excluded_entry_type", "entry type excluded"),
            ("semantic_git_admin", "Git admin excluded"),
            ("record_errors", "record errors"),
        )
        extras = [f"{label}={counts[name]:,}" for name, label in outcomes if counts[name]]
        details.append(line + ("; " + ", ".join(extras) if extras else ""))
    if collection.filesystem_result is not None:
        pruned = collection.filesystem_result.accounting.pruned_ignored_subtrees
        if pruned:
            details.append(pruned_ignored_subtree_label(pruned))

    timestamp_counts: dict[EvidenceKind, Counter[str]] = {}
    for item in ledger.timestamps:
        evidence_kind = EvidenceKind.from_dimensions(
            item.key.record_kind,
            item.key.timestamp_kind,
            item.key.entry_type,
        )
        counts = timestamp_counts.setdefault(evidence_kind, Counter())
        counts.update(
            examined=item.examined,
            values_read=item.values_read,
            scope_matches=item.scope_matches,
            scope_errors=item.scope_errors,
            materialization_errors=item.materialization_errors,
            selected=item.selected,
            markers=item.markers,
            unavailable=item.unavailable,
            unsupported=item.unsupported,
            errors=item.extraction_errors,
            coalesced=item.coalesced_into_markers,
        )
    for kind in EvidenceKind:
        if kind not in timestamp_counts:
            continue
        counts = timestamp_counts[kind]
        line = f"{kind.value} selected: {counts['selected']:,}"
        extras = [
            f"{name.replace('_', ' ')}={counts[name]:,}"
            for name in (
                "examined",
                "values_read",
                "scope_matches",
                "scope_errors",
                "materialization_errors",
                "markers",
                "unavailable",
                "unsupported",
                "errors",
                "coalesced",
            )
            if counts[name]
        ]
        details.append(line + ("; " + ", ".join(extras) if extras else ""))

    plan = CollectionPlan.from_selection(options.evidence)
    if collection.commit_result is not None and plan.file_change_timestamps:
        commit_result = collection.commit_result
        examined_commits = sum(item.examined_commits for item in commit_result.repository_accounting)
        candidate_commits = sum(item.candidate_commits for item in commit_result.repository_accounting)
        selected_commits = sum(item.selected_commits for item in commit_result.repository_accounting)
        hydrated_commits = sum(item.hydrated_commits for item in commit_result.repository_accounting)
        scope_evaluation_errors = sum(
            count for item in commit_result.repository_accounting for _role, count in item.scope_evaluation_errors
        )
        if not commit_result.repository_accounting:
            examined_commits = candidate_commits = selected_commits = hydrated_commits = len(commit_result.commits)
        details.append(
            "Git commit inputs for file-change derivation: "
            f"reachable={commit_result.discovered_commit_ids:,}, "
            f"examined={examined_commits:,}, candidates={candidate_commits:,}, "
            f"hydrated={hydrated_commits:,}, selected={selected_commits:,}, "
            f"scope evaluation errors={scope_evaluation_errors:,}, "
            f"record errors={sum(item.record_errors for item in commit_result.repository_accounting):,}"
        )
        for accounting in commit_result.repository_accounting:
            target_scope_errors = sum(count for _role, count in accounting.scope_evaluation_errors)
            details.append(
                f"target Git commit inputs [git] {accounting.repository.root}: "
                f"reachable={accounting.discovered_commit_ids:,}, "
                f"examined={accounting.examined_commits:,}, "
                f"candidates={accounting.candidate_commits:,}, "
                f"hydrated={accounting.hydrated_commits:,}, "
                f"selected={accounting.selected_commits:,}, "
                f"scope evaluation errors={target_scope_errors:,}, "
                f"unavailable={accounting.unavailable_objects:,}, "
                f"parse failures={accounting.parse_errors:,}, "
                f"operational errors={accounting.operational_errors:,}"
            )

    if collection.file_change_result is not None:
        file_result = collection.file_change_result
        details.append(
            "Git file-change derivation: "
            f"commits requested={file_result.requested_commits:,}, "
            f"successfully parsed={file_result.successful_commits:,}, "
            f"parse failures={file_result.parse_errors:,}, "
            f"subprocess failures={file_result.subprocess_errors:,}, "
            f"file changes discovered={file_result.discovered_changes:,}"
        )
        for accounting in file_result.repository_accounting:
            details.append(
                f"target Git file-change derivation [git] {accounting.repository.root}: "
                f"commits requested={accounting.requested_commits:,}, "
                f"successfully parsed={accounting.successful_commits:,}, "
                f"parse failures={accounting.parse_errors:,}, "
                f"subprocess failures={accounting.subprocess_errors:,}, "
                f"file changes discovered={accounting.discovered_changes:,}"
            )

    for item in ledger.records:
        outcomes = (
            f"discovered={item.discovered:,}, eligible={item.eligible:,}, "
            f"ignored={item.ignored:,}, explicitly excluded={item.explicitly_excluded:,}, "
            f"entry type excluded={item.excluded_entry_type:,}, "
            f"Git admin excluded={item.semantic_git_admin:,}, errors={item.record_errors:,}"
        )
        details.append(
            f"target records [{item.key.source.value}] {item.key.target} {item.key.record_kind.value}: {outcomes}"
        )
    for item in ledger.timestamps:
        outcomes = (
            f"examined={item.examined:,}, values read={item.values_read:,}, "
            f"unavailable={item.unavailable:,}, unsupported={item.unsupported:,}, "
            f"errors={item.extraction_errors:,}, scope matches={item.scope_matches:,}, "
            f"scope errors={item.scope_errors:,}, "
            f"materialization errors={item.materialization_errors:,}, selected={item.selected:,}, "
            f"markers={item.markers:,}, coalesced={item.coalesced_into_markers:,}"
        )
        evidence_kind = EvidenceKind.from_dimensions(
            item.key.record_kind,
            item.key.timestamp_kind,
            item.key.entry_type,
        )
        details.append(
            f"target timestamps [{item.key.source.value}] {item.key.target} {evidence_kind.value}: {outcomes}"
        )

    if collection.commit_result is not None and collection.commit_result.duplicate_commit_ids:
        details.append(f"duplicate commit IDs deduplicated: {collection.commit_result.duplicate_commit_ids:,}")
    duplicate_targets = 0
    if collection.commit_result is not None:
        duplicate_targets += collection.commit_result.duplicate_targets
        if collection.commit_result.repository_accounting:
            shared_contexts = len(collection.commit_result.repositories) - len(
                collection.commit_result.repository_accounting
            )
            if shared_contexts > 0:
                details.append(f"linked worktree contexts sharing commit history: {shared_contexts:,}")
    if collection.repository_resolution is not None:
        duplicate_targets += collection.repository_resolution.duplicate_targets
    if duplicate_targets:
        details.append(f"duplicate selected Git targets deduplicated: {duplicate_targets:,}")
    if collection.tag_result is not None:
        details.append(
            f"tags: {collection.tag_result.annotated_tags:,} annotated, "
            f"{collection.tag_result.lightweight_tags:,} lightweight"
        )
    if collection.reflog_result is not None:
        details.append(
            f"reflogs: {len(collection.reflog_result.available_refs):,} available, "
            f"{len(collection.reflog_result.refs_without_reflog):,} unavailable"
        )
    if collection.filesystem_result is not None and collection.filesystem_result.overlapping_roots_deduplicated:
        details.append(
            "overlapping filesystem roots deduplicated: "
            f"{collection.filesystem_result.overlapping_roots_deduplicated:,}"
        )
    for capability in collection.capabilities:
        if capability.status is not CapabilityStatus.SUPPORTED or capability.note:
            note = f" ({capability.note})" if capability.note else ""
            details.append(f"{capability.name}: {capability.status.value}{note}")
    if collection.diagnostics:
        errors, warnings, infos = collection.diagnostic_counts
        counts = [f"{errors:,} error(s)", f"{warnings:,} warning(s)"]
        if infos:
            counts.append(f"{infos:,} info message(s)")
        details.append("operational diagnostics: " + ", ".join(counts))
    return tuple(details)
