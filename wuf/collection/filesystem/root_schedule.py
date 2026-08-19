"""Non-overlapping ownership and ordering for filesystem scan roots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wuf.collection.filesystem.entries import is_lexical_descendant
from wuf.collection.filesystem.ignore import ExplicitExcluder
from wuf.collection.filesystem.scan import RootSnapshot


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


@dataclass(frozen=True, slots=True)
class RootScanTask:
    """One exact root and the exclusion policy scoped to that root."""

    snapshot: RootSnapshot
    excluder: ExplicitExcluder


class ExplicitRootOwnership:
    """Partition overlapping repository scopes around explicit selections.

    Ordinary lexical overlaps are removed before this policy is built. The
    remaining overlaps necessarily cross repository boundaries. An explicit
    descendant keeps ownership of its exact path or subtree; automatically
    discovered repository roots must leave that scope for the explicit task.
    This makes ownership independent of whether either scan later succeeds.
    """

    def __init__(self, roots: tuple[RootSnapshot, ...]) -> None:
        self._roots = tuple(item.path for item in roots)

    def scope_for(self, current_root: Path) -> RootOwnershipScope:
        """Build a cheap relative-path policy for one scheduled root."""

        prefixes: list[tuple[str, ...]] = []
        current_key = _path_key(current_root)
        for explicit_root in self._roots:
            if _path_key(explicit_root) == current_key or not is_lexical_descendant(
                explicit_root,
                current_root,
            ):
                continue
            relative = PurePosixPath(explicit_root.relative_to(current_root).as_posix())
            prefixes.append(tuple(os.path.normcase(part) for part in relative.parts))
        return RootOwnershipScope(tuple(prefixes))


@dataclass(frozen=True, slots=True)
class RootOwnershipScope:
    """Relative explicit-root prefixes delegated away from one scan root."""

    delegated_prefixes: tuple[tuple[str, ...], ...]

    def delegates(self, relative: PurePosixPath) -> bool:
        """Return whether another explicit root owns *relative*."""

        if not self.delegated_prefixes:
            return False
        parts = tuple(os.path.normcase(part) for part in relative.parts)
        return any(parts[: len(prefix)] == prefix for prefix in self.delegated_prefixes)


class RootScanSchedule:
    """Order explicit and discovered roots without speculative supersession."""

    def __init__(
        self,
        roots: tuple[RootSnapshot, ...],
        scan_roots: tuple[Path, ...],
        excluder: ExplicitExcluder,
    ) -> None:
        self._tasks = [RootScanTask(item, excluder) for item in roots]
        self._scheduled = {_path_key(item.path) for item in roots}
        self._unresolved_roots = tuple(path for path in scan_roots if _path_key(path) not in self._scheduled)
        self._cursor = 0

    @property
    def scan_roots(self) -> tuple[Path, ...]:
        """Return every explicit or discovered root in stable discovery order."""

        return (*(task.snapshot.path for task in self._tasks), *self._unresolved_roots)

    def take(self) -> RootScanTask | None:
        """Return the next task, or ``None`` when the schedule is exhausted."""

        if self._cursor >= len(self._tasks):
            return None
        task = self._tasks[self._cursor]
        self._cursor += 1
        return task

    def add_discovered(
        self,
        roots: tuple[tuple[RootSnapshot, ExplicitExcluder], ...],
    ) -> None:
        """Insert newly discovered repositories before later explicit tasks."""

        accepted: list[RootScanTask] = []
        for snapshot, excluder in roots:
            key = _path_key(snapshot.path)
            if key in self._scheduled:
                continue
            self._scheduled.add(key)
            accepted.append(RootScanTask(snapshot, excluder))
        if accepted:
            self._tasks[self._cursor : self._cursor] = accepted


__all__ = ["ExplicitRootOwnership", "RootOwnershipScope", "RootScanSchedule", "RootScanTask"]
