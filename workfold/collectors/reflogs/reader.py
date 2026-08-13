"""Safe, no-follow semantic reflog path handling and reads."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from workfold.collectors.git_core.repository import GitRepository
from workfold.collectors.reflogs.models import GitReflogReadError


def decode_git_path(payload: bytes, *, repository: GitRepository) -> Path:
    if not payload.endswith(b"\n"):
        raise GitReflogReadError("invalid_git_reflog_path", "Git returned an invalid reflog path")
    raw_path = payload[:-1]
    if not raw_path or b"\0" in raw_path:
        raise GitReflogReadError("invalid_git_reflog_path", "Git returned an invalid reflog path")
    path = Path(os.fsdecode(raw_path))
    return path if path.is_absolute() else repository.root / path


def open_semantic_reflog(path: Path, *, repository: GitRepository) -> tuple[int, os.stat_result, Path]:
    """Open a repository-owned regular reflog without following the final path."""

    descriptor = -1
    try:
        resolved = path.resolve(strict=True)
        allowed_roots = tuple(
            root.resolve(strict=True) for root in dict.fromkeys((repository.git_dir, repository.common_dir))
        )
        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise GitReflogReadError(
                "unsafe_git_reflog_path",
                "Git resolved a reflog outside the selected repository metadata",
                path=path,
            )
        resolved_snapshot = os.lstat(resolved)
        if not stat.S_ISREG(resolved_snapshot.st_mode):
            raise GitReflogReadError(
                "invalid_git_reflog_file",
                "Git resolved a reflog that is not a regular file",
                path=resolved,
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(resolved, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GitReflogReadError(
                "invalid_git_reflog_file",
                "Git resolved a reflog that is not a regular file",
                path=resolved,
            )
        opened_descriptor = descriptor
        descriptor = -1
        return opened_descriptor, before, resolved
    except GitReflogReadError:
        raise
    except OSError as error:
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def snapshot_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def read_semantic_reflog(path: Path, *, repository: GitRepository) -> tuple[bytes, bool]:
    """Read one regular reflog safely and report concurrent mutation."""

    descriptor, before, _resolved = open_semantic_reflog(path, repository=repository)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read()
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, snapshot_changed(before, after)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
