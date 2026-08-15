"""Terminal descriptions of configured collection scope."""

from __future__ import annotations

from workfold.application.report import CollectionFacts, ReportScope
from workfold.domain.coverage import CapabilityKind, CapabilityStatus
from workfold.domain.observations import EntryType, RecordKind, Source, TimestampKind
from workfold.domain.scope import RefScope


def source_label(scope: ReportScope) -> str:
    events = ", ".join(kind.value for kind in scope.evidence.kinds)
    parts = [events]
    if any(kind.record_kind in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE} for kind in scope.evidence.kinds):
        reachability = {
            RefScope.HEAD: "HEAD",
            RefScope.LOCAL_BRANCHES: "local branches + detached HEAD",
            RefScope.ALL_REFS: "all locally stored refs",
        }[scope.ref_scope]
        parts.append(f"commits from {reachability}")
    if any(kind.timestamp_kind is TimestampKind.FS_ACCESSED for kind in scope.evidence.kinds):
        parts.append("atime potentially unreliable")
    return "; ".join(parts)


def extent_label(collection: CollectionFacts, scope: ReportScope) -> str | None:
    parts: list[str] = []
    if scope.includes_source(Source.GIT) and collection.git_roots:
        parts.append("whole Git repositories=" + ", ".join(collection.git_roots))
    if scope.includes_source(Source.FILESYSTEM) and collection.filesystem_roots:
        parts.append("exact filesystem roots=" + ", ".join(collection.filesystem_roots))
    return "; ".join(parts) or None


def identity_label(scope: ReportScope) -> str | None:
    if not scope.includes_source(Source.GIT):
        return None
    if not scope.git_identities:
        return "all recorded identities"
    filters = " OR ".join(scope.git_identities)
    suffix = "; filesystem unaffected" if scope.includes_source(Source.FILESYSTEM) else ""
    return f"{filters}{suffix}"


def ignore_label(scope: ReportScope, collection: CollectionFacts) -> str | None:
    if not scope.includes_source(Source.FILESYSTEM):
        return None
    entry_names = {
        EntryType.REGULAR_FILE: "files",
        EntryType.DIRECTORY: "directories",
        EntryType.SYMLINK: "symlinks",
    }
    selected_types = {kind.entry_type for kind in scope.evidence.kinds if kind.entry_type is not None}
    entry_scope = " + ".join(entry_names[item] for item in EntryType if item in selected_types)
    if scope.include_ignored:
        policy = "ignored entries included"
    else:
        capabilities = tuple(
            item for item in collection.capabilities if item.kind is CapabilityKind.GIT_IGNORE_SEMANTICS
        )
        notes = tuple(item.note or "" for item in capabilities)
        if capabilities and all("outside a Git worktree" in note for note in notes):
            policy = "outside a Git worktree; no Git ignore rules apply"
        elif capabilities and any(item.status is CapabilityStatus.UNAVAILABLE for item in capabilities):
            policy = "standard Git ignore policy partially unavailable"
        elif capabilities and any("outside a Git worktree" in note for note in notes):
            policy = "standard Git ignores respected where applicable"
        else:
            policy = "standard Git ignores respected"
    return f"{policy}; {entry_scope}"


__all__ = ["extent_label", "identity_label", "ignore_label", "source_label"]
