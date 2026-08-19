"""Terminal labels for requested coverage scope."""

from __future__ import annotations

from wuf.application.report import ReportScope
from wuf.domain.observations import RecordKind, Source, TimestampKind


def enabled_sources(scope: ReportScope) -> tuple[Source, ...]:
    return scope.sources


def coverage_scope_details(scope: ReportScope) -> tuple[str, ...]:
    requested = ", ".join(kind.value for kind in scope.evidence.kinds)
    return (f"scope event kinds: requested={requested}",)


def record_label(kind: RecordKind) -> str:
    return {
        RecordKind.COMMIT: "Git commits",
        RecordKind.GIT_FILE_CHANGE: "Git file changes",
        RecordKind.TAG: "Git tags",
        RecordKind.REFLOG: "Git reflog entries",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }[kind]


def timestamp_label(kind: TimestampKind) -> str:
    if kind is TimestampKind.FS_CREATED:
        return "filesystem birth"
    return kind.value.replace("git_", "Git ").replace("fs_", "filesystem ").replace("_", " ")


def pruned_ignored_subtree_label(count: int) -> str:
    noun = "subtree" if count == 1 else "subtrees"
    return f"{count:,} ignored filesystem {noun} pruned; descendant directories not counted"


__all__ = [
    "coverage_scope_details",
    "enabled_sources",
    "pruned_ignored_subtree_label",
    "record_label",
    "timestamp_label",
]
