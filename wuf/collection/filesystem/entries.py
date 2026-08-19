"""Shared filesystem collection classification and diagnostics helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from wuf.collection.diagnostics import (
    CollectorDiagnostic,
    DiagnosticCategory,
    DiagnosticKind,
    DiagnosticSeverity,
)
from wuf.collection.filesystem.ignore import (
    GitIgnoreProbe,
    GitIgnoreRepository,
    is_git_admin_name,
    is_git_admin_path,
    is_nested_repository_boundary,
    is_within_git_admin,
)
from wuf.collection.filesystem.models import CollectedFilesystemEntry
from wuf.collection.filesystem.scan import DirectorySafetyError, PendingEntry
from wuf.domain.coverage import (
    Capability,
    CapabilityKind,
    CapabilityReason,
    CapabilityStatus,
    RecordDisposition,
)
from wuf.domain.observations import EntryType, RecordKind, RecordOrigin, Source
from wuf.domain.provenance import absolute_filesystem_entry_id


def retain_entry(
    entries: list[CollectedFilesystemEntry] | None,
    origin: RecordOrigin,
    disposition: RecordDisposition,
) -> None:
    if entries is not None:
        entries.append(CollectedFilesystemEntry(origin, disposition))


def entry_type(mode: int) -> EntryType | None:
    if stat.S_ISREG(mode):
        return EntryType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return EntryType.DIRECTORY
    if stat.S_ISLNK(mode):
        return EntryType.SYMLINK
    return None


def is_semantic_git_admin(
    path: Path,
    repository: GitIgnoreRepository | None,
    *,
    relative_parts: tuple[str, ...] = (),
    admin_relative_parts: tuple[str, ...] = (),
) -> bool:
    if admin_relative_parts and relative_parts[: len(admin_relative_parts)] == admin_relative_parts:
        return True
    if not is_git_admin_name(path):
        return False
    return (repository is not None and is_within_git_admin(path, repository)) or is_git_admin_path(path)


def repository_admin_relative_parts(root: Path, repository: GitIgnoreRepository | None) -> tuple[str, ...]:
    if repository is None or repository.admin_root is None:
        return ()
    try:
        physical_root = root.resolve(strict=True)
        physical_admin = repository.admin_root.resolve(strict=True)
        relative = physical_admin.relative_to(physical_root)
    except (OSError, RuntimeError, ValueError):
        return ()
    return PurePosixPath(relative.as_posix()).parts


def entry_is_in_scope(
    candidate_type: EntryType | None,
    *,
    include_regular_files: bool,
    include_directories: bool,
    include_symlinks: bool,
) -> bool:
    if candidate_type is EntryType.REGULAR_FILE:
        return include_regular_files
    if candidate_type is EntryType.DIRECTORY:
        return include_directories
    if candidate_type is EntryType.SYMLINK:
        return include_symlinks
    return False


def origin(root: Path, path: Path, candidate_type: EntryType | None) -> RecordOrigin:
    type_name = candidate_type.value if candidate_type is not None else "special"
    return RecordOrigin(
        record_id=absolute_filesystem_entry_id(root, path, type_name),
        source=Source.FILESYSTEM,
        record_kind=RecordKind.FILESYSTEM_ENTRY,
        repository_or_root=root,
        path=path,
        entry_type=candidate_type,
    )


def pending_origin(item: PendingEntry) -> RecordOrigin:
    """Return a retained origin or materialize it after scope selection."""

    return item.origin if item.origin is not None else origin(item.root, item.path, item.entry_type)


def is_lexical_descendant(path: Path, parent: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(parent)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(os.fspath(parent)) and os.path.normcase(
        os.fspath(path)
    ) != os.path.normcase(os.fspath(parent))


def crosses_nested_repository(path: Path, parent: Path) -> bool:
    """Return whether deduplicating *path* would cross a repository boundary."""

    candidate = path
    while candidate != parent and is_lexical_descendant(candidate, parent):
        if is_nested_repository_boundary(candidate, selected_root=parent):
            return True
        candidate = candidate.parent
    return False


def stat_diagnostic(root: Path, path: Path, error: OSError, *, is_root: bool) -> CollectorDiagnostic:
    code = "path_not_found" if isinstance(error, FileNotFoundError) and is_root else "filesystem_stat_error"
    return CollectorDiagnostic(
        code=code,
        stage="filesystem_root_resolution" if is_root else "filesystem_stat",
        target=os.fspath(root),
        message=f"filesystem metadata could not be read: {error}",
        path=os.fspath(path),
        category=(DiagnosticCategory.INVOCATION if code == "path_not_found" else DiagnosticCategory.COLLECTION),
    )


def traversal_diagnostic(root: Path, path: Path, error: OSError) -> CollectorDiagnostic:
    failed_path = (
        os.fsdecode(error.filename)
        if isinstance(error, DirectorySafetyError) and isinstance(error.filename, (str, bytes))
        else os.fspath(path)
    )
    return CollectorDiagnostic(
        code="filesystem_concurrent_mutation"
        if isinstance(error, DirectorySafetyError)
        else "filesystem_traversal_error",
        stage="filesystem_traversal",
        target=os.fspath(root),
        message=f"directory could not be fully enumerated: {error}",
        path=failed_path,
    )


def ignore_diagnostic(root: Path, error: Exception, *, warning: bool) -> CollectorDiagnostic:
    code = getattr(error, "code", "git_ignore_unavailable")
    incomplete_inventory = code == "git_filesystem_inventory_incomplete"
    hint = (
        None if incomplete_inventory else "Install/repair Git or use --include-ignored to request an unfiltered scan."
    )
    return CollectorDiagnostic(
        code=code,
        stage="filesystem_ignore_discovery",
        target=os.fspath(root),
        message=str(error),
        severity=DiagnosticSeverity.WARNING if warning or incomplete_inventory else DiagnosticSeverity.ERROR,
        path=os.fspath(root),
        hint=hint,
        affects_completeness=incomplete_inventory,
        kind=DiagnosticKind.FILESYSTEM_INVENTORY if incomplete_inventory else DiagnosticKind.GENERAL,
    )


def ignore_capability(
    root: Path,
    respect_gitignore: bool,
    probe: GitIgnoreProbe,
    *,
    error: Exception | None,
) -> Capability:
    reason: CapabilityReason | None = None
    if not respect_gitignore:
        status = CapabilityStatus.SUPPORTED
        note = "ignored entries were explicitly included"
    elif error is not None:
        status = CapabilityStatus.UNAVAILABLE
        note = str(error)
    elif probe.capability_reason is not None:
        status = CapabilityStatus.NOT_APPLICABLE
        note = probe.note
        reason = probe.capability_reason
    elif probe.repository is None:
        status = CapabilityStatus.UNAVAILABLE
        note = probe.note
    else:
        status = CapabilityStatus.SUPPORTED
        note = probe.note
    return Capability(
        source=Source.FILESYSTEM,
        target=os.fspath(root),
        kind=CapabilityKind.GIT_IGNORE_SEMANTICS,
        name="standard Git ignore semantics",
        status=status,
        note=note,
        reason=reason,
    )
