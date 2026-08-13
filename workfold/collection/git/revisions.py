"""Git revision-scope enumeration across ordinary and linked worktrees."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Final

from workfold.collection.git.objects.models import RevListScanSpec
from workfold.collection.git.repository import GitRepository
from workfold.collection.git.runner import GitCommandError, GitRunner
from workfold.domain.scope import RefScope

OID_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def parse_commit_ids(output: bytes, *, repository: GitRepository) -> tuple[tuple[str, ...], int]:
    """Parse and deduplicate validated object IDs from buffered Git output."""

    ids: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for raw_line in output.splitlines():
        object_id = _validated_object_id(raw_line, repository=repository, command=("rev-list",))
        if object_id in seen:
            duplicates += 1
            continue
        seen.add(object_id)
        ids.append(object_id)
    return tuple(ids), duplicates


def enumerate_commit_ids(
    repository: GitRepository,
    runner: GitRunner,
    ref_scope: RefScope,
) -> tuple[tuple[str, ...], int]:
    """Materialize the streaming enumerator for compatibility callers."""

    ids: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for object_id in iter_commit_ids(repository, runner, ref_scope):
        if object_id in seen:
            duplicates += 1
            continue
        seen.add(object_id)
        ids.append(object_id)
    return tuple(ids), duplicates


def iter_commit_ids(repository: GitRepository, runner: GitRunner, ref_scope: RefScope) -> Iterator[str]:
    """Stream commits reachable through one worktree context."""

    yield from iter_commit_ids_for_contexts((repository,), runner, ref_scope)


def iter_commit_ids_for_contexts(
    repositories: Sequence[GitRepository],
    runner: GitRunner,
    ref_scope: RefScope,
) -> Iterator[str]:
    """Enumerate one shared history from every selected worktree context.

    Branch and ordinary ref storage is shared by linked worktrees, while HEAD
    can be worktree-local and detached. Resolve each selected HEAD first, then
    ask one representative context to traverse the union exactly once.
    """

    revisions = _revision_arguments_for_contexts(repositories, runner, ref_scope)
    if revisions is None:
        return
    yield from _iter_validated_commit_ids(repositories[0], runner, revisions)


def iter_commit_scans_for_contexts(
    repositories: Sequence[GitRepository],
    runner: GitRunner,
    ref_scope: RefScope,
    spec: RevListScanSpec,
) -> Iterator[bytes]:
    """Stream only the fields required for bounded-range selection."""

    revisions = _revision_arguments_for_contexts(repositories, runner, ref_scope)
    if revisions is None:
        return
    command = (
        revisions[0],
        "--no-commit-header",
        "--encoding=none",
        f"--format={spec.pretty_format}",
        *revisions[1:],
    )
    yield from runner.iter_stdout_lines(command, cwd=repositories[0].root)


def _revision_arguments_for_contexts(
    repositories: Sequence[GitRepository],
    runner: GitRunner,
    ref_scope: RefScope,
) -> tuple[str, ...] | None:
    if not repositories:
        return None
    if len(repositories) == 1 and ref_scope is RefScope.ALL_REFS:
        return ("rev-list", "--all")

    head_ids: list[str] = []
    seen_heads: set[str] = set()
    head_command = ("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    for context in repositories:
        head = runner.run(head_command, cwd=context.root, allowed_returncodes=(0, 1))
        if head.returncode == 1:
            continue
        object_id = _validated_object_id(
            head.stdout.rstrip(b"\r\n"),
            repository=context,
            command=head_command,
        )
        if object_id not in seen_heads:
            seen_heads.add(object_id)
            head_ids.append(object_id)

    if len(repositories) == 1:
        if ref_scope is RefScope.HEAD:
            if not head_ids:
                return None
            revisions = ("rev-list", *head_ids)
        else:
            revisions = ("rev-list", "--branches", *head_ids)
    elif ref_scope is RefScope.ALL_REFS:
        revisions = ("rev-list", "--all", *head_ids)
    elif ref_scope is RefScope.LOCAL_BRANCHES:
        revisions = ("rev-list", "--branches", *head_ids)
    elif head_ids:
        revisions = ("rev-list", *head_ids)
    else:
        return None
    return revisions


def _iter_validated_commit_ids(
    repository: GitRepository,
    runner: GitRunner,
    revisions: tuple[str, ...],
) -> Iterator[str]:
    for raw_line in runner.iter_stdout_lines(revisions, cwd=repository.root):
        yield _validated_object_id(raw_line.rstrip(b"\r\n"), repository=repository, command=revisions)


def _validated_object_id(
    raw_object_id: bytes,
    *,
    repository: GitRepository,
    command: tuple[str, ...],
) -> str:
    try:
        object_id = raw_object_id.decode("ascii")
    except UnicodeDecodeError as error:
        raise GitCommandError(
            code="invalid_git_output",
            message=f"git {command[0]} returned a non-ASCII object ID",
            command=command,
            cwd=repository.root,
        ) from error
    if not OID_TEXT_RE.fullmatch(object_id):
        raise GitCommandError(
            code="invalid_git_output",
            message=f"git {command[0]} returned an invalid object ID",
            command=command,
            cwd=repository.root,
        )
    return object_id


__all__ = [
    "enumerate_commit_ids",
    "iter_commit_ids",
    "iter_commit_ids_for_contexts",
    "iter_commit_scans_for_contexts",
    "parse_commit_ids",
]
