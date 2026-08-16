"""Verbose terminal coverage reconciliation."""

from __future__ import annotations

from collections import Counter

from workfold.application.report import CollectionFacts, ReportScope
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
    collection: CollectionFacts,
    scope: ReportScope,
) -> tuple[str, ...]:
    """Render verbose coverage accounting for terminal presentation."""

    details: list[str] = [
        f"timestamp slots examined: {ledger.slots_examined:,}",
        f"timestamp values read: {ledger.timestamp_values_read:,}",
        f"timestamp values matching scope: {ledger.timestamp_values_matching_scope:,}",
        f"timestamp observations selected: {ledger.observations_selected:,}",
        f"activity markers plotted: {ledger.markers_plotted:,}",
        *coverage_scope_details(scope),
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
    if collection.pruned_ignored_subtrees:
        details.append(pruned_ignored_subtree_label(collection.pruned_ignored_subtrees))

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

    if collection.commit_inputs is not None:
        commit_inputs = collection.commit_inputs
        details.append(
            "Git commit inputs for file-change derivation: "
            f"reachable={commit_inputs.reachable:,}, "
            f"examined={commit_inputs.examined:,}, candidates={commit_inputs.candidates:,}, "
            f"hydrated={commit_inputs.hydrated:,}, selected={commit_inputs.selected:,}, "
            f"scope evaluation errors={commit_inputs.scope_evaluation_errors:,}, "
            f"record errors={commit_inputs.record_errors:,}"
        )
        for target in commit_inputs.targets:
            details.append(
                f"target Git commit inputs [git] {target.root}: "
                f"reachable={target.reachable:,}, "
                f"examined={target.examined:,}, "
                f"candidates={target.candidates:,}, "
                f"hydrated={target.hydrated:,}, "
                f"selected={target.selected:,}, "
                f"scope evaluation errors={target.scope_evaluation_errors:,}, "
                f"unavailable={target.unavailable:,}, "
                f"parse failures={target.parse_failures:,}, "
                f"operational errors={target.operational_errors:,}"
            )

    if collection.file_changes is not None:
        file_changes = collection.file_changes
        details.append(
            "Git file-change derivation: "
            f"commits requested={file_changes.commits_requested:,}, "
            f"successfully parsed={file_changes.successfully_parsed:,}, "
            f"parse failures={file_changes.parse_failures:,}, "
            f"subprocess failures={file_changes.subprocess_failures:,}, "
            f"file changes discovered={file_changes.changes_discovered:,}"
        )
        for target in file_changes.targets:
            details.append(
                f"target Git file-change derivation [git] {target.root}: "
                f"commits requested={target.commits_requested:,}, "
                f"successfully parsed={target.successfully_parsed:,}, "
                f"parse failures={target.parse_failures:,}, "
                f"subprocess failures={target.subprocess_failures:,}, "
                f"file changes discovered={target.changes_discovered:,}"
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

    if collection.duplicate_commit_ids:
        details.append(f"duplicate commit IDs deduplicated: {collection.duplicate_commit_ids:,}")
    if collection.linked_worktree_contexts:
        details.append(f"linked worktree contexts sharing commit history: {collection.linked_worktree_contexts:,}")
    if collection.duplicate_git_targets:
        details.append(f"duplicate selected Git targets deduplicated: {collection.duplicate_git_targets:,}")
    if collection.tags is not None:
        details.append(f"tags: {collection.tags.annotated:,} annotated, {collection.tags.lightweight:,} lightweight")
    if collection.reflogs is not None:
        details.append(
            f"reflogs: {collection.reflogs.available:,} available, {collection.reflogs.unavailable:,} unavailable"
        )
    if collection.overlapping_filesystem_roots:
        details.append(f"overlapping filesystem roots deduplicated: {collection.overlapping_filesystem_roots:,}")
    for capability in collection.capabilities:
        if capability.status is not CapabilityStatus.SUPPORTED or capability.note:
            note = f" ({capability.note})" if capability.note else ""
            status = capability.status.value.replace("_", " ")
            details.append(f"{capability.name}: {status}{note}")
    diagnostic_facts = collection.diagnostics
    if diagnostic_facts.errors or diagnostic_facts.warnings or diagnostic_facts.infos:
        errors, warnings, infos = diagnostic_facts.errors, diagnostic_facts.warnings, diagnostic_facts.infos
        counts = [f"{errors:,} error(s)", f"{warnings:,} warning(s)"]
        if infos:
            counts.append(f"{infos:,} info message(s)")
        details.append("operational diagnostics: " + ", ".join(counts))
    return tuple(details)
