"""Resolve selected paths to safe, deduplicated local repository contexts."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from wuf.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer, DiagnosticCategory
from wuf.collection.git.runner import GitCommandError, GitRunner, command_diagnostic


@dataclass(frozen=True, slots=True)
class GitRepository:
    """Resolved repository extent for a selected input path."""

    root: Path
    git_dir: Path
    common_dir: Path
    is_bare: bool

    @property
    def identity(self) -> str:
        """Canonical shared-history identity for this repository."""

        return os.fspath(self.common_dir)

    @property
    def context_identity(self) -> str:
        """Canonical identity for this repository's worktree-local context."""

        return os.fspath(self.git_dir)


@dataclass(frozen=True, slots=True)
class GitRepositoryResolutionResult:
    """Unique repositories resolved from an exact set of requested paths."""

    repositories: tuple[GitRepository, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_targets: int
    successful_targets: int
    duplicate_targets: int

    @property
    def is_partial(self) -> bool:
        return self.successful_targets != self.requested_targets


def resolve_repository(path: Path, runner: GitRunner) -> GitRepository:
    """Resolve any existing file/directory path to its whole containing repository."""

    expanded = path.expanduser()
    try:
        selected = expanded.resolve(strict=True)
    except FileNotFoundError as error:
        raise GitCommandError(
            code="path_not_found",
            message=f"selected path does not exist: {expanded}",
            command=(),
            cwd=expanded.parent,
            hint="Pass an existing file or directory.",
            category=DiagnosticCategory.INVOCATION,
        ) from error
    probe = selected if selected.is_dir() else selected.parent

    try:
        bare_output = runner.run(("rev-parse", "--is-bare-repository"), cwd=probe).stdout.strip()
    except GitCommandError as error:
        if error.code == "git_command_failed":
            raise GitCommandError(
                code="not_git_repository",
                message=f"not a Git repository: {selected}",
                command=error.command,
                cwd=probe,
                returncode=error.returncode,
                stderr=error.stderr,
                stderr_truncated=error.stderr_truncated,
                hint="Use --profile fs or pass a path inside a Git repository.",
                category=DiagnosticCategory.INVOCATION,
            ) from error
        raise
    if bare_output not in {b"true", b"false"}:
        raise GitCommandError(
            code="invalid_git_output",
            message="Git returned an invalid bare-repository status",
            command=("rev-parse", "--is-bare-repository"),
            cwd=probe,
        )
    is_bare = bare_output == b"true"

    try:
        git_dir = _resolve_git_output_path(
            runner.run(("rev-parse", "--absolute-git-dir"), cwd=probe).stdout,
            probe=probe,
        )
        common_dir = _resolve_git_output_path(
            runner.run(("rev-parse", "--git-common-dir"), cwd=probe).stdout,
            probe=probe,
        )
        root = (
            git_dir
            if is_bare
            else _resolve_git_output_path(
                runner.run(("rev-parse", "--show-toplevel"), cwd=probe).stdout,
                probe=probe,
            )
        )
    except GitCommandError:
        raise
    except (OSError, ValueError) as error:
        raise GitCommandError(
            code="invalid_git_output",
            message=f"Git returned an invalid repository path: {error}",
            command=("rev-parse",),
            cwd=probe,
        ) from error
    return GitRepository(root=root, git_dir=git_dir, common_dir=common_dir, is_bare=is_bare)


def unique_semantic_repositories(repositories: Sequence[GitRepository]) -> tuple[GitRepository, ...]:
    """Keep one traversal context for each shared Git object/ref database."""

    unique: list[GitRepository] = []
    seen: set[str] = set()
    for repository in repositories:
        if repository.identity in seen:
            continue
        seen.add(repository.identity)
        unique.append(repository)
    return tuple(unique)


def group_semantic_repositories(
    repositories: Sequence[GitRepository],
) -> tuple[tuple[GitRepository, ...], ...]:
    """Group worktree-local contexts that share one object/ref database."""

    groups: list[list[GitRepository]] = []
    indexes: dict[str, int] = {}
    for repository in repositories:
        index = indexes.get(repository.identity)
        if index is None:
            indexes[repository.identity] = len(groups)
            groups.append([repository])
        else:
            groups[index].append(repository)
    return tuple(tuple(group) for group in groups)


class GitRepositoryResolver:
    """Resolve selected paths without traversing refs or objects."""

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._runner = runner or GitRunner()

    def resolve(self, paths: Sequence[Path]) -> GitRepositoryResolutionResult:
        diagnostics = DiagnosticBuffer()
        repositories: list[GitRepository] = []
        seen_repositories: set[str] = set()
        successful_targets = 0
        duplicate_targets = 0

        for path in paths:
            try:
                repository = resolve_repository(path, self._runner)
            except GitCommandError as error:
                diagnostics.append(command_diagnostic(error, stage="git_repository_resolution", target=path))
                continue
            successful_targets += 1
            if repository.context_identity in seen_repositories:
                duplicate_targets += 1
                continue
            seen_repositories.add(repository.context_identity)
            repositories.append(repository)

        return GitRepositoryResolutionResult(
            repositories=tuple(repositories),
            diagnostics=diagnostics.snapshot(),
            requested_targets=len(paths),
            successful_targets=successful_targets,
            duplicate_targets=duplicate_targets,
        )


def _resolve_git_output_path(payload: bytes, *, probe: Path) -> Path:
    path = _decode_path(payload)
    return (path if path.is_absolute() else probe / path).resolve()


def _decode_path(value: bytes) -> Path:
    if not value.endswith(b"\n"):
        raise ValueError("Git path response has no record terminator")
    raw_path = value[:-1]
    if not raw_path or b"\0" in raw_path:
        raise ValueError("Git path response is empty or contains NUL")
    return Path(os.fsdecode(raw_path))
