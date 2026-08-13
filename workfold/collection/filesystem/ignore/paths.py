"""No-follow Git repository-boundary and administrative-path checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Final

from workfold.collection.filesystem.ignore.models import GitIgnoreRepository
from workfold.domain.provenance import lexical_absolute

MAX_GITDIR_POINTER_BYTES: Final[int] = 4096


def is_git_admin_name(path: Path) -> bool:
    """Return whether an entry is the conventional Git administrative node."""

    return path.name == ".git"


def is_git_admin_path(path: Path) -> bool:
    """Return whether ``path`` is a plausible repository administrative node."""

    return is_git_admin_name(path) and _looks_like_worktree_marker(path)


def is_within_git_admin(path: Path, repository: GitIgnoreRepository) -> bool:
    """Match Git's authoritative admin directory without following ``path`` itself."""

    if repository.admin_root is None:
        return False
    try:
        admin_root = repository.admin_root.resolve(strict=True)
        physical_path = physical_path_without_following_final(path)
    except (OSError, RuntimeError):
        return False
    if is_same_or_descendant(physical_path, admin_root):
        return True
    if is_git_admin_name(path):
        try:
            final_target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return is_same_or_descendant(final_target, admin_root)
    return False


def has_git_admin_ancestor(path: Path) -> bool:
    """Detect lexical or physical Git storage without following ``path`` itself."""

    if _has_plausible_admin_ancestor(path):
        return True
    try:
        physical_path = physical_path_without_following_final(path)
    except (OSError, RuntimeError):
        return False
    return physical_path != path and _has_plausible_admin_ancestor(physical_path)


def has_repository_marker_ancestor(path: Path) -> bool:
    """Conservatively find a worktree marker or bare repository above a path."""

    for candidate in (path, *path.parents):
        if _looks_like_worktree_marker(candidate / ".git") or looks_like_bare_repository(candidate):
            return True
    return False


def is_nested_repository_boundary(path: Path, *, selected_root: Path) -> bool:
    """Detect a nested worktree/submodule/bare repository without following links."""

    if path == selected_root:
        return False
    if _looks_like_worktree_marker(path / ".git"):
        return True
    return looks_like_bare_repository(path)


def looks_like_bare_repository(path: Path) -> bool:
    """Conservatively recognize the mandatory shape of bare Git storage."""

    return (
        _is_mode(path / "HEAD", stat.S_ISREG)
        and _is_directory_or_symlink(path / "objects")
        and _is_directory_or_symlink(path / "refs")
    )


def physical_path_without_following_final(path: Path) -> Path:
    """Resolve ancestors for evaluation while retaining the final directory entry."""

    return path.parent.resolve(strict=True) / path.name


def is_same_or_descendant(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def parse_absolute_git_path(value: bytes, *, field: str) -> Path:
    if not value.endswith(b"\n"):
        raise ValueError(f"{field} has no record terminator")
    raw_path = value[:-1]
    if not raw_path or b"\0" in raw_path or b"\n" in raw_path or b"\r" in raw_path:
        raise ValueError(f"empty or unsafe {field}")
    path = lexical_absolute(os.fsdecode(raw_path))
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"unresolvable {field}: {error}") from error


def _looks_like_worktree_marker(path: Path) -> bool:
    """Recognize a plausible worktree marker without following symlinks."""

    try:
        snapshot = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISREG(snapshot.st_mode):
        target = _read_gitdir_pointer(path, snapshot)
        return target is not None and _looks_like_git_directory(target)
    return stat.S_ISDIR(snapshot.st_mode) and _looks_like_git_directory(path)


def _looks_like_git_directory(path: Path) -> bool:
    return _is_mode(path / "HEAD", stat.S_ISREG) and (
        (_is_directory_or_symlink(path / "objects") and _is_directory_or_symlink(path / "refs"))
        or _is_mode(path / "commondir", stat.S_ISREG)
    )


def _read_gitdir_pointer(path: Path, snapshot: os.stat_result) -> Path | None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            snapshot.st_dev,
            snapshot.st_ino,
        ):
            return None
        value = os.read(descriptor, MAX_GITDIR_POINTER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(value) > MAX_GITDIR_POINTER_BYTES or not value.startswith(b"gitdir: "):
        return None
    raw_target = value[len(b"gitdir: ") :].rstrip(b"\r\n")
    if not raw_target or b"\0" in raw_target or b"\n" in raw_target or b"\r" in raw_target:
        return None
    target = Path(os.fsdecode(raw_target))
    if not target.is_absolute():
        target = path.parent / target
    try:
        return target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _has_plausible_admin_ancestor(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if is_git_admin_path(candidate) or looks_like_bare_repository(candidate):
            return True
    return False


def _is_mode(path: Path, predicate: Callable[[int], bool]) -> bool:
    try:
        snapshot = os.lstat(path)
    except OSError:
        return False
    return predicate(snapshot.st_mode)


def _is_directory_or_symlink(path: Path) -> bool:
    try:
        snapshot = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(snapshot.st_mode) or stat.S_ISLNK(snapshot.st_mode)
