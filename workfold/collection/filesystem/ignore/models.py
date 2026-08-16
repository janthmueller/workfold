"""Value objects and structured failures for Git-aware filesystem discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from workfold.domain.coverage import CapabilityReason


class ExclusionPatternError(ValueError):
    """Raised when an explicit exclusion exceeds the documented subset."""


class GitIgnoreCommandError(RuntimeError):
    """A bounded, structured failure from Git ignore plumbing."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        cwd: Path,
        command: tuple[str, ...],
        returncode: int | None = None,
        stderr: bytes = b"",
        unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.cwd = cwd
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        self.unavailable = unavailable

    @property
    def stderr_text(self) -> str:
        """Decode subprocess stderr without discarding invalid bytes."""

        return self.stderr.decode("utf-8", errors="surrogateescape").rstrip()


@dataclass(frozen=True, slots=True)
class GitIgnoreRepository:
    """The local worktree or bare-repository context for one scan root."""

    root: Path
    is_bare: bool
    admin_root: Path | None = None


@dataclass(frozen=True, slots=True)
class GitIgnoreProbe:
    """Result of locating applicable standard Git ignore semantics."""

    repository: GitIgnoreRepository | None
    git_available: bool
    note: str
    error: GitIgnoreCommandError | None = None
    capability_reason: CapabilityReason | None = None


@dataclass(frozen=True, slots=True)
class IgnoreCandidate:
    """One path to evaluate with ``git check-ignore``."""

    path: Path
    is_directory: bool


@dataclass(frozen=True, slots=True)
class GitIgnoreMatches:
    """Ignored paths or a structured inability to determine them."""

    ignored_paths: frozenset[Path]
    error: GitIgnoreCommandError | None = None


@dataclass(frozen=True, slots=True)
class GitFilesystemInventory:
    """Git-authoritative path candidates for one current worktree scope."""

    included_relative_paths: tuple[str, ...] = ()
    ignored_relative_paths: tuple[str, ...] = ()
    ignored_directory_paths: frozenset[str] = frozenset()
    warning: GitIgnoreCommandError | None = None
    error: GitIgnoreCommandError | None = None

    def __post_init__(self) -> None:
        included = set(self.included_relative_paths)
        ignored = set(self.ignored_relative_paths)
        normalized_included = {os.path.normcase(path) for path in included}
        normalized_ignored = {os.path.normcase(path) for path in ignored}
        if len(included) != len(self.included_relative_paths) or len(normalized_included) != len(included):
            raise ValueError("Git filesystem inventory contains duplicate included paths")
        if len(ignored) != len(self.ignored_relative_paths) or len(normalized_ignored) != len(ignored):
            raise ValueError("Git filesystem inventory contains duplicate ignored paths")
        if normalized_included & normalized_ignored:
            raise ValueError("Git filesystem inventory cannot include and ignore the same path")
        if included & self.ignored_directory_paths:
            raise ValueError("Git filesystem inventory cannot include an ignored directory path")
        normalized_directories = {
            tuple(os.path.normcase(part) for part in directory.split("/")) for directory in self.ignored_directory_paths
        }
        for included_path in included:
            parts = tuple(os.path.normcase(part) for part in included_path.split("/"))
            if any(parts[:size] in normalized_directories for size in range(1, len(parts) + 1)):
                raise ValueError("Git filesystem inventory cannot place included paths below an ignored directory")
        if self.warning is not None and self.error is not None:
            raise ValueError("Git filesystem inventory cannot carry both a warning and a fatal error")


class GitFilesystemInventoryView(Protocol):
    """Bounded membership view used while traversing a current worktree."""

    def ignore_state(self, relative_path: str) -> tuple[bool, bool]:
        """Return ``(ignored path, ignored-directory boundary)``."""

        ...


@dataclass(frozen=True, slots=True)
class GitFilesystemInventoryVisit:
    """Outcome of a disk-backed, callback-driven Git path inventory."""

    included_paths: int = 0
    ignored_paths: int = 0
    warning: GitIgnoreCommandError | None = None
    error: GitIgnoreCommandError | None = None

    def __post_init__(self) -> None:
        if self.included_paths < 0 or self.ignored_paths < 0:
            raise ValueError("streamed inventory counts must be non-negative")
        if self.warning is not None and self.error is not None:
            raise ValueError("a streamed inventory cannot carry both a warning and an error")
