"""Terminal labels for requested coverage scope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TypeVar

from workfold.application.resolution import filesystem_timestamp_kinds, git_timestamp_kinds
from workfold.configuration.options import RunOptions
from workfold.domain.observations import RecordKind, Source, TimestampKind

_ScopeValue = TypeVar("_ScopeValue", Source, RecordKind, TimestampKind)


def enabled_sources(options: RunOptions) -> tuple[Source, ...]:
    sources: list[Source] = []
    if options.source.includes_git:
        sources.append(Source.GIT)
    if options.source.includes_filesystem:
        sources.append(Source.FILESYSTEM)
    return tuple(sources)


def enabled_record_kinds(options: RunOptions) -> tuple[RecordKind, ...]:
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


def coverage_scope_details(options: RunOptions) -> tuple[str, ...]:
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
    timestamp_names = {kind: timestamp_label(kind) for kind in TimestampKind}
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


def record_label(kind: RecordKind) -> str:
    return {
        RecordKind.COMMIT: "Git commits",
        RecordKind.GIT_FILE_CHANGE: "Git file changes",
        RecordKind.TAG: "Git tags",
        RecordKind.REFLOG: "Git reflog entries",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }[kind]


def timestamp_label(kind: TimestampKind) -> str:
    return kind.value.replace("git_", "Git ").replace("fs_", "filesystem ").replace("_", " ")


def pruned_ignored_subtree_label(count: int) -> str:
    noun = "subtree" if count == 1 else "subtrees"
    return f"{count:,} ignored filesystem {noun} pruned; descendant directories not counted"


__all__ = [
    "coverage_scope_details",
    "enabled_record_kinds",
    "enabled_sources",
    "pruned_ignored_subtree_label",
    "record_label",
    "timestamp_label",
]
