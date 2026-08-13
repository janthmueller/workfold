"""Filesystem exclusion patterns and standard Git ignore integration."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from workfold.collection.filesystem.ignore.exclusions import ExplicitExcluder
from workfold.collection.filesystem.ignore.inventory import build_inventory, inspect_inventory, visit_inventory
from workfold.collection.filesystem.ignore.models import (
    ExclusionPatternError,
    GitFilesystemInventory,
    GitFilesystemInventoryView,
    GitFilesystemInventoryVisit,
    GitIgnoreCommandError,
    GitIgnoreMatches,
    GitIgnoreProbe,
    GitIgnoreRepository,
    IgnoreCandidate,
)
from workfold.collection.filesystem.ignore.paths import (
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_git_admin_name,
    is_git_admin_path,
    is_nested_repository_boundary,
    is_within_git_admin,
    looks_like_bare_repository,
    parse_absolute_git_path,
    physical_path_without_following_final,
)
from workfold.collection.filesystem.ignore.runner import GitIgnoreRunner

InventoryBuilder = Callable[[GitIgnoreRunner, GitIgnoreRepository, Path], GitFilesystemInventory]
InventoryVisitor = Callable[..., GitFilesystemInventoryVisit]
InventoryInspector = Callable[..., GitFilesystemInventoryVisit]


class GitIgnoreService:
    """Discover worktrees and ask Git for its authoritative ignore decisions."""

    def __init__(
        self,
        runner: GitIgnoreRunner | None = None,
        *,
        inventory_builder: InventoryBuilder = build_inventory,
        inventory_visitor: InventoryVisitor | None = visit_inventory,
        inventory_inspector: InventoryInspector | None = None,
        transactional_inventory: bool = True,
    ) -> None:
        self._runner = runner or GitIgnoreRunner()
        self._inventory_builder = inventory_builder
        self._inventory_visitor = inventory_visitor
        self._inventory_inspector = (
            inventory_inspector
            if inventory_inspector is not None
            else inspect_inventory
            if transactional_inventory
            else None
        )
        self._transactional_inventory = transactional_inventory

    @property
    def transactional_inventory(self) -> bool:
        """Whether an inventory failure is guaranteed to precede callbacks."""

        return self._transactional_inventory

    def probe(self, path: Path, *, is_directory: bool) -> GitIgnoreProbe:
        """Locate the containing worktree without treating non-repos as errors."""

        cwd = path if is_directory else path.parent
        try:
            state = self._runner.run(
                ("rev-parse", "--is-inside-work-tree", "--is-bare-repository"),
                cwd=cwd,
                allowed_returncodes=(0, 128),
            )
        except GitIgnoreCommandError as error:
            return GitIgnoreProbe(None, not error.unavailable, str(error), error)
        if state.returncode == 128:
            if not has_repository_marker_ancestor(cwd):
                return GitIgnoreProbe(None, True, "outside a Git worktree; no Git ignore rules apply")
            error = GitIgnoreCommandError(
                code="git_repository_probe_failed",
                message="Git could not inspect a repository marker visible above the selected path",
                cwd=cwd,
                command=("rev-parse", "--is-inside-work-tree", "--is-bare-repository"),
                returncode=state.returncode,
                stderr=state.stderr,
            )
            return GitIgnoreProbe(None, True, str(error), error)
        values = state.stdout.rstrip(b"\r\n").splitlines()
        if values not in ([b"true", b"false"], [b"false", b"true"], [b"false", b"false"]):
            error = GitIgnoreCommandError(
                code="git_ignore_parse_error",
                message="Git returned an unexpected repository-state response",
                cwd=cwd,
                command=("rev-parse", "--is-inside-work-tree", "--is-bare-repository"),
                stderr=state.stderr,
            )
            return GitIgnoreProbe(None, True, str(error), error)
        try:
            admin_result = self._runner.run(("rev-parse", "--absolute-git-dir"), cwd=cwd)
            admin_root = parse_absolute_git_path(admin_result.stdout, field="administrative directory")
            if values == [b"false", b"true"]:
                return GitIgnoreProbe(
                    GitIgnoreRepository(admin_root, True, admin_root),
                    True,
                    "selected path is inside bare Git administrative storage",
                )
            if values == [b"false", b"false"]:
                return GitIgnoreProbe(
                    GitIgnoreRepository(admin_root, False, admin_root),
                    True,
                    "selected path is inside non-bare Git administrative storage",
                )
            top_level = self._runner.run(("rev-parse", "--show-toplevel"), cwd=cwd)
            root = parse_absolute_git_path(top_level.stdout, field="worktree root")
        except (GitIgnoreCommandError, ValueError) as error:
            structured = (
                error
                if isinstance(error, GitIgnoreCommandError)
                else GitIgnoreCommandError(
                    code="git_ignore_parse_error",
                    message=f"could not parse Git repository paths: {error}",
                    cwd=cwd,
                    command=("rev-parse", "--absolute-git-dir"),
                )
            )
            return GitIgnoreProbe(None, True, str(structured), structured)
        return GitIgnoreProbe(
            GitIgnoreRepository(root, False, admin_root),
            True,
            "standard repository, nested, info, and global Git excludes are active",
        )

    def ignored(
        self,
        repository: GitIgnoreRepository,
        candidates: Sequence[IgnoreCandidate],
        *,
        lexical_root: Path | None = None,
    ) -> GitIgnoreMatches:
        """Return Git's ignored subset; tracked entries remain unignored."""

        encoded_to_paths: dict[bytes, list[Path]] = {}
        mapping_failures: list[tuple[Path, str]] = []
        try:
            physical_repository_root = repository.root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            structured = GitIgnoreCommandError(
                code="git_ignore_path_mapping_error",
                message=f"could not map the Git worktree root into physical coordinates: {error}",
                cwd=repository.root,
                command=("check-ignore", "--stdin", "-z"),
            )
            return GitIgnoreMatches(frozenset(), structured)
        physical_lexical_root: Path | None = None
        if lexical_root is not None:
            try:
                physical_lexical_root = physical_path_without_following_final(lexical_root)
                physical_lexical_root.relative_to(physical_repository_root)
            except (OSError, RuntimeError, ValueError) as error:
                structured = GitIgnoreCommandError(
                    code="git_ignore_path_mapping_error",
                    message=f"could not map the selected root into the physical Git worktree: {error}",
                    cwd=repository.root,
                    command=("check-ignore", "--stdin", "-z"),
                )
                return GitIgnoreMatches(frozenset(), structured)
        for candidate in candidates:
            try:
                if lexical_root is None or physical_lexical_root is None:
                    physical_path = physical_path_without_following_final(candidate.path)
                else:
                    relative_to_root = candidate.path.relative_to(lexical_root)
                    physical_path = physical_lexical_root / relative_to_root
                relative = physical_path.relative_to(physical_repository_root)
            except (OSError, RuntimeError, ValueError) as error:
                mapping_failures.append((candidate.path, str(error)))
                continue
            if relative == Path("."):
                continue
            relative_bytes = os.fsencode(relative.as_posix())
            if candidate.is_directory:
                relative_bytes += b"/"
            encoded_to_paths.setdefault(relative_bytes, []).append(candidate.path)
        mapping_error = _mapping_error(repository, mapping_failures)
        if not encoded_to_paths:
            return GitIgnoreMatches(frozenset(), mapping_error)
        payload = b"\0".join(encoded_to_paths) + b"\0"
        try:
            completed = self._runner.run(
                ("check-ignore", "--stdin", "-z"),
                cwd=repository.root,
                input_data=payload,
                allowed_returncodes=(0, 1),
            )
        except GitIgnoreCommandError as error:
            return GitIgnoreMatches(frozenset(), error)
        output_tokens = tuple(token for token in completed.stdout.split(b"\0") if token)
        unknown = tuple(token for token in output_tokens if token not in encoded_to_paths)
        if unknown:
            error = GitIgnoreCommandError(
                code="git_ignore_parse_error",
                message="Git returned an ignore path that was not requested",
                cwd=repository.root,
                command=("check-ignore", "--stdin", "-z"),
            )
            return GitIgnoreMatches(frozenset(), error)
        return GitIgnoreMatches(
            frozenset(path for token in output_tokens for path in encoded_to_paths[token]),
            mapping_error,
        )

    def inventory(self, repository: GitIgnoreRepository, selected_root: Path) -> GitFilesystemInventory:
        """Return current leaf candidates using standard Git ignore semantics.

        The inventory is an optimization boundary, not filesystem evidence:
        tracked index paths may be absent from the worktree, so the collector
        must validate every included path with a current no-follow stat.
        """

        return self._inventory_builder(self._runner, repository, selected_root)

    def visit_inventory(
        self,
        repository: GitIgnoreRepository,
        selected_root: Path,
        *,
        included_consumer: Callable[[str], None],
        ignored_consumer: Callable[[str, bool], None],
    ) -> GitFilesystemInventoryVisit:
        """Visit a complete Git inventory without retaining every path in RAM.

        Git output is parsed into an ephemeral SQLite spool first. Consumers
        run only after all three commands and every path validate, so a late
        command or parse failure can fall back without duplicating records.
        A deliberately injected materialized visitor remains available for
        tests and integrations that cannot stream.
        """

        if self._inventory_visitor is None:
            inventory = self.inventory(repository, selected_root)
            if inventory.error is not None:
                return GitFilesystemInventoryVisit(error=inventory.error)
            for path in inventory.included_relative_paths:
                included_consumer(path)
            for path in inventory.ignored_relative_paths:
                ignored_consumer(path, path in inventory.ignored_directory_paths)
            return GitFilesystemInventoryVisit(
                included_paths=len(inventory.included_relative_paths),
                ignored_paths=len(inventory.ignored_relative_paths),
                warning=inventory.warning,
            )

        return self._inventory_visitor(
            self._runner,
            repository,
            selected_root,
            included_consumer=included_consumer,
            ignored_consumer=ignored_consumer,
        )

    def inspect_inventory(
        self,
        repository: GitIgnoreRepository,
        selected_root: Path,
        *,
        inventory_consumer: Callable[[GitFilesystemInventoryView], None],
        unseen_ignored_consumer: Callable[[str, bool], None],
    ) -> GitFilesystemInventoryVisit:
        """Provide bounded ignore membership while a native traversal runs."""

        if self._inventory_inspector is None:
            inventory = self.inventory(repository, selected_root)
            if inventory.error is not None:
                return GitFilesystemInventoryVisit(error=inventory.error)
            view = _MaterializedInventoryView(inventory)
            inventory_consumer(view)
            for path, is_directory in view.unseen_ignored():
                unseen_ignored_consumer(path, is_directory)
            return GitFilesystemInventoryVisit(
                included_paths=len(inventory.included_relative_paths),
                ignored_paths=len(inventory.ignored_relative_paths),
                warning=inventory.warning,
            )

        return self._inventory_inspector(
            self._runner,
            repository,
            selected_root,
            inventory_consumer=inventory_consumer,
            unseen_ignored_consumer=unseen_ignored_consumer,
        )


class _MaterializedInventoryView:
    """Compatibility membership view for injected non-streaming inventories."""

    def __init__(self, inventory: GitFilesystemInventory) -> None:
        self._ignored = {os.path.normcase(path): path for path in inventory.ignored_relative_paths}
        self._directories = {os.path.normcase(path) for path in inventory.ignored_directory_paths}
        self._seen: set[str] = set()

    def ignore_state(self, relative_path: str) -> tuple[bool, bool]:
        key = os.path.normcase(relative_path)
        ignored = key in self._ignored
        if ignored:
            self._seen.add(key)
        return ignored, key in self._directories

    def unseen_ignored(self) -> tuple[tuple[str, bool], ...]:
        return tuple((path, key in self._directories) for key, path in self._ignored.items() if key not in self._seen)


def _mapping_error(
    repository: GitIgnoreRepository,
    failures: Sequence[tuple[Path, str]],
) -> GitIgnoreCommandError | None:
    if not failures:
        return None
    first_path, first_detail = failures[0]
    suffix = "" if len(failures) == 1 else f" (and {len(failures) - 1} more path(s))"
    return GitIgnoreCommandError(
        code="git_ignore_path_mapping_error",
        message=(
            f"could not map ignore candidate {os.fspath(first_path)!r} into the physical Git worktree: "
            f"{first_detail}{suffix}"
        ),
        cwd=repository.root,
        command=("check-ignore", "--stdin", "-z"),
    )


__all__ = [
    "ExclusionPatternError",
    "ExplicitExcluder",
    "GitFilesystemInventory",
    "GitFilesystemInventoryVisit",
    "GitIgnoreCommandError",
    "GitIgnoreMatches",
    "GitIgnoreProbe",
    "GitIgnoreRepository",
    "GitIgnoreRunner",
    "GitIgnoreService",
    "IgnoreCandidate",
    "has_git_admin_ancestor",
    "has_repository_marker_ancestor",
    "is_git_admin_name",
    "is_git_admin_path",
    "is_within_git_admin",
    "is_nested_repository_boundary",
    "looks_like_bare_repository",
]
