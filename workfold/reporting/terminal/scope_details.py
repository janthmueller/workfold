"""Terminal descriptions of configured collection scope."""

from __future__ import annotations

import os

from workfold.application.collection import Collection
from workfold.configuration.options import FilesystemEntry, FilesystemTime, GitDateMode, RefScope, RunOptions
from workfold.domain.coverage import CapabilityStatus


def source_label(options: RunOptions) -> str:
    parts: list[str] = []
    if options.source.includes_git:
        records: list[str] = []
        if options.git_records.includes_commits:
            if options.git_mode.includes_commit_markers:
                records.append("commits")
            if options.git_mode.includes_file_changes:
                records.append("file changes")
        if options.git_records.includes_tags:
            records.append("tags")
        if options.git_records.includes_reflogs:
            records.append("reflogs")
        roles = {
            GitDateMode.AUTHOR: "author dates",
            GitDateMode.COMMITTER: "committer dates",
            GitDateMode.BOTH: "author + committer dates",
        }[options.git_date]
        reachability = {
            RefScope.HEAD: "HEAD",
            RefScope.LOCAL_BRANCHES: "local branches + detached HEAD",
            RefScope.ALL_REFS: "all locally stored refs",
        }[options.ref_scope]
        suffix = f", {roles}, commits from {reachability}" if options.git_records.includes_commits else ""
        parts.append(f"Git {' + '.join(records)}{suffix}")
    if options.source.includes_filesystem:
        time_names = {
            FilesystemTime.CREATED: "birth",
            FilesystemTime.MODIFIED: "modified",
            FilesystemTime.CHANGED: "metadata-changed",
            FilesystemTime.ACCESSED: "accessed",
        }
        times = ",".join(time_names[item] for item in options.filesystem_times)
        reliability = "; atime potentially unreliable" if FilesystemTime.ACCESSED in options.filesystem_times else ""
        parts.append(f"filesystem ({times}{reliability})")
    return "; ".join(parts)


def extent_label(collection: Collection, options: RunOptions) -> str | None:
    parts: list[str] = []
    if options.source.includes_git:
        repositories = (
            collection.commit_result.repositories
            if collection.commit_result is not None
            else (collection.repository_resolution.repositories if collection.repository_resolution is not None else ())
        )
        roots = tuple(dict.fromkeys(os.fspath(item.root) for item in repositories))
        if roots:
            parts.append("whole Git repositories=" + ", ".join(roots))
    if options.source.includes_filesystem and collection.filesystem_result is not None:
        roots = tuple(os.fspath(item) for item in collection.filesystem_result.scan_roots)
        if roots:
            parts.append("exact filesystem roots=" + ", ".join(roots))
    return "; ".join(parts) or None


def identity_label(options: RunOptions) -> str | None:
    if not options.source.includes_git:
        return None
    if not options.git_identities:
        return "all recorded identities"
    filters = " OR ".join(options.git_identities)
    suffix = "; filesystem unaffected" if options.source.includes_filesystem else ""
    return f"{filters}{suffix}"


def ignore_label(options: RunOptions, collection: Collection) -> str | None:
    if not options.source.includes_filesystem:
        return None
    entry_names = {
        FilesystemEntry.FILE: "files",
        FilesystemEntry.DIRECTORY: "directories",
        FilesystemEntry.SYMLINK: "symlinks",
    }
    entry_scope = " + ".join(entry_names[item] for item in options.filesystem_entries)
    if options.include_ignored:
        policy = "ignored entries included"
    else:
        capabilities = tuple(item for item in collection.capabilities if item.name == "standard Git ignore semantics")
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
