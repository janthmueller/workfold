"""Describe coverage scope and reconciliation for terminal reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TypeVar

from workfold.app.collection import Collection
from workfold.app.resolution import filesystem_timestamp_kinds, git_timestamp_kinds
from workfold.collectors.base import DiagnosticSeverity
from workfold.config import RawOptions
from workfold.coverage import CapabilityStatus, CoverageLedger
from workfold.models import RecordKind, Source, TimestampKind
from workfold.reports import COMPLETE_COVERAGE_STATUS

_ScopeValue = TypeVar("_ScopeValue", Source, RecordKind, TimestampKind)


def enabled_sources(options: RawOptions) -> tuple[Source, ...]:
    """Return the source kinds enabled by the resolved options."""

    sources: list[Source] = []
    if options.source.includes_git:
        sources.append(Source.GIT)
    if options.source.includes_filesystem:
        sources.append(Source.FILESYSTEM)
    return tuple(sources)


def enabled_record_kinds(options: RawOptions) -> tuple[RecordKind, ...]:
    """Return the record kinds enabled by the resolved options."""

    kinds: list[RecordKind] = []
    if options.source.includes_git:
        if options.git_records.includes_commits and options.git_mode.includes_commit_markers:
            kinds.append(RecordKind.COMMIT)
        if options.git_records.includes_commits and options.git_mode.includes_file_changes:
            kinds.append(RecordKind.GIT_FILE_CHANGE)
        if options.git_records.includes_tags:
            kinds.append(RecordKind.TAG)
        if options.git_records.includes_reflogs:
            kinds.append(RecordKind.REFLOG)
    if options.source.includes_filesystem:
        kinds.append(RecordKind.FILESYSTEM_ENTRY)
    return tuple(kinds)


def coverage_status_label(
    collection: Collection,
    ledger: CoverageLedger,
    options: RawOptions,
) -> str:
    """Summarize whether enabled collectors completed their requested scope."""

    error_count = sum(item.severity is DiagnosticSeverity.ERROR for item in collection.diagnostics)
    if error_count or ledger.has_operational_errors:
        label = f"partial ({error_count} collection error(s))"
    else:
        label = COMPLETE_COVERAGE_STATUS
    qualifiers: list[str] = []
    if options.git_identities:
        qualifiers.append("Git timestamps explicitly filtered by identity")
    if options.exclusions:
        qualifiers.append("explicit exclusions active")
    unsupported_capabilities: dict[str, str | None] = {}
    for capability in collection.capabilities:
        if capability.status is CapabilityStatus.UNSUPPORTED:
            unsupported_capabilities.setdefault(capability.name, capability.note)
    for name, note in unsupported_capabilities.items():
        qualifier = f"{name} unavailable"
        if note:
            qualifier += f": {note}"
        qualifiers.append(qualifier)
    if qualifiers:
        label += "; " + "; ".join(qualifiers)
    return label


def coverage_details(
    ledger: CoverageLedger,
    collection: Collection,
    options: RawOptions,
) -> tuple[str, ...]:
    """Render verbose coverage accounting without coupling it to a renderer."""

    details: list[str] = [
        f"timestamp slots requested: {ledger.slots_requested:,}",
        f"timestamp observations captured: {ledger.observations_captured:,}",
        f"timestamp observations included: {ledger.observations_included:,}",
        f"activity markers plotted: {ledger.markers_plotted:,}",
        *_coverage_scope_details(options),
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
        line = f"{_record_label(kind)} discovered: {counts['discovered']:,}"
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

    timestamp_counts: dict[TimestampKind, Counter[str]] = {}
    for item in ledger.timestamps:
        counts = timestamp_counts.setdefault(item.key.timestamp_kind, Counter())
        counts.update(
            requested=item.requested,
            captured=item.captured,
            included=item.included,
            markers=item.markers,
            unavailable=item.unavailable,
            unsupported=item.unsupported,
            errors=item.extraction_errors,
            outside_date=item.outside_date,
            identity_filtered=item.identity_filtered,
            coalesced=item.coalesced_into_markers,
        )
    for kind in sorted(timestamp_counts, key=lambda item: item.value):
        counts = timestamp_counts[kind]
        line = f"{_timestamp_label(kind)} captured: {counts['captured']:,}"
        extras = [
            f"{name.replace('_', ' ')}={counts[name]:,}"
            for name in (
                "requested",
                "included",
                "markers",
                "unavailable",
                "unsupported",
                "errors",
                "outside_date",
                "identity_filtered",
                "coalesced",
            )
            if counts[name]
        ]
        details.append(line + ("; " + ", ".join(extras) if extras else ""))

    if (
        collection.commit_result is not None
        and options.git_mode.includes_file_changes
        and not options.git_mode.includes_commit_markers
    ):
        commit_result = collection.commit_result
        captured_commits = sum(item.captured_commits for item in commit_result.repository_accounting)
        if not commit_result.repository_accounting:
            captured_commits = len(commit_result.commits)
        details.append(
            "Git commit inputs for file-change derivation: "
            f"discovered={commit_result.discovered_commit_ids:,}, "
            f"parsed={captured_commits:,}, "
            f"record errors={commit_result.discovered_commit_ids - captured_commits:,}"
        )
        for accounting in commit_result.repository_accounting:
            details.append(
                f"target Git commit inputs [git] {accounting.repository.root}: "
                f"discovered={accounting.discovered_commit_ids:,}, "
                f"parsed={accounting.captured_commits:,}, "
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
            f"requested={item.requested:,}, captured={item.captured:,}, "
            f"unavailable={item.unavailable:,}, unsupported={item.unsupported:,}, "
            f"errors={item.extraction_errors:,}, included={item.included:,}, "
            f"outside date={item.outside_date:,}, identity filtered={item.identity_filtered:,}, "
            f"markers={item.markers:,}, coalesced={item.coalesced_into_markers:,}"
        )
        details.append(
            f"target timestamps [{item.key.source.value}] {item.key.target} "
            f"{item.key.record_kind.value}/{item.key.timestamp_kind.value}: {outcomes}"
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
        errors = sum(item.severity is DiagnosticSeverity.ERROR for item in collection.diagnostics)
        warnings = len(collection.diagnostics) - errors
        details.append(f"operational diagnostics: {errors:,} error(s), {warnings:,} warning(s)")
    return tuple(details)


def _coverage_scope_details(options: RawOptions) -> tuple[str, ...]:
    requested_sources = set(enabled_sources(options))
    requested_records = set(enabled_record_kinds(options))
    requested_timestamps: set[TimestampKind] = set()
    if options.source.includes_git:
        if options.git_records.includes_commits:
            requested_timestamps.update(git_timestamp_kinds(options.git_date))
        if options.git_records.includes_tags:
            requested_timestamps.add(TimestampKind.GIT_TAGGER)
        if options.git_records.includes_reflogs:
            requested_timestamps.add(TimestampKind.GIT_REFLOG)
    if options.source.includes_filesystem:
        requested_timestamps.update(filesystem_timestamp_kinds(options.filesystem_times))

    source_names = {Source.GIT: "git", Source.FILESYSTEM: "filesystem"}
    record_names = {
        RecordKind.COMMIT: "commits",
        RecordKind.GIT_FILE_CHANGE: "file changes",
        RecordKind.TAG: "tags",
        RecordKind.REFLOG: "reflogs",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }
    timestamp_names = {kind: _timestamp_label(kind) for kind in TimestampKind}
    return (
        _scope_line("sources", tuple(Source), requested_sources, source_names),
        _scope_line("record kinds", tuple(RecordKind), requested_records, record_names),
        _scope_line("timestamp kinds", tuple(TimestampKind), requested_timestamps, timestamp_names),
    )


def _scope_line(
    label: str,
    all_values: Sequence[_ScopeValue],
    requested: AbstractSet[_ScopeValue],
    names: Mapping[_ScopeValue, str],
) -> str:
    requested_text = ", ".join(names[item] for item in all_values if item in requested)
    omitted_text = ", ".join(names[item] for item in all_values if item not in requested)
    return f"scope {label}: requested={requested_text or 'none'}; not requested={omitted_text or 'none'}"


def _record_label(kind: RecordKind) -> str:
    return {
        RecordKind.COMMIT: "Git commits",
        RecordKind.GIT_FILE_CHANGE: "Git file changes",
        RecordKind.TAG: "Git tags",
        RecordKind.REFLOG: "Git reflog entries",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }[kind]


def _timestamp_label(kind: TimestampKind) -> str:
    return kind.value.replace("git_", "Git ").replace("fs_", "filesystem ").replace("_", " ")
