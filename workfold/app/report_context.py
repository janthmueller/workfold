"""Build collector-neutral report metadata from resolved options and accounting."""

from __future__ import annotations

import os

from workfold.app.collection import Collection
from workfold.app.coverage_text import (
    coverage_details,
    coverage_status_label,
    enabled_record_kinds,
    enabled_sources,
)
from workfold.config import FilesystemEntry, FilesystemTime, GitDateMode, RawOptions, RefScope
from workfold.coverage import CapabilityStatus, CoverageLedger
from workfold.reports import ReportContext


def build_report_context(
    collection: Collection,
    options: RawOptions,
    ledger: CoverageLedger,
    *,
    range_label: str,
    timezone_label: str,
    schedule_label: str,
) -> ReportContext:
    """Assemble presentation metadata without coupling it to terminal rendering."""

    return ReportContext(
        source_label=_source_label(options),
        range_label=range_label,
        timezone_label=timezone_label,
        schedule_label=schedule_label,
        coverage_status=coverage_status_label(collection, ledger, options),
        profile_label=options.profile.value,
        extent_label=_extent_label(collection, options),
        enabled_sources=enabled_sources(options),
        enabled_record_kinds=enabled_record_kinds(options),
        identity_label=_identity_label(options),
        ignore_label=_ignore_label(options, collection),
        exclusions=options.exclusions,
        coverage_details=coverage_details(ledger, collection, options) if options.coverage or options.verbose else (),
    )


def _source_label(options: RawOptions) -> str:
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


def _extent_label(collection: Collection, options: RawOptions) -> str | None:
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


def _identity_label(options: RawOptions) -> str | None:
    if not options.source.includes_git:
        return None
    if not options.git_identities:
        return "all recorded identities"
    filters = " OR ".join(options.git_identities)
    suffix = "; filesystem unaffected" if options.source.includes_filesystem else ""
    return f"{filters}{suffix}"


def _ignore_label(options: RawOptions, collection: Collection) -> str | None:
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
