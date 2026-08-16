"""Git filesystem-inventory commands, path decoding, and diagnostics."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Final

from workfold.collection.filesystem.ignore.models import GitIgnoreCommandError, GitIgnoreRepository

MAX_INVENTORY_STDERR_BYTES: Final[int] = 16_384
INVENTORY_PATHS_NEED_NORMALIZATION: Final[bool] = os.path.normcase("A/B") != "A/B"


class InventoryStreamDecoder:
    """Turn arbitrary stdout chunks into validated NUL-delimited paths."""

    def __init__(self, selected_prefix: Path, consumer: Callable[[bytes, bool], None]) -> None:
        self._prefix_parts = _inventory_prefix_parts(selected_prefix)
        self._consumer = consumer
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer.extend(chunk)
        start = 0
        while True:
            end = self._buffer.find(0, start)
            if end < 0:
                break
            raw_path = bytes(self._buffer[start:end])
            selected_relative, directory_hint = _parse_inventory_record(
                raw_path,
                prefix_parts=self._prefix_parts,
            )
            self._consumer(selected_relative, directory_hint)
            start = end + 1
        if start:
            del self._buffer[:start]

    def finish(self) -> None:
        if self._buffer:
            raise ValueError("NUL-delimited Git output has no final terminator")


def resolve_scope(
    repository: GitIgnoreRepository,
    selected_root: Path,
) -> tuple[Path, Path] | GitIgnoreCommandError:
    try:
        physical_repository_root = repository.root.resolve(strict=True)
        physical_selected_root = selected_root.resolve(strict=True)
        selected_prefix = physical_selected_root.relative_to(physical_repository_root)
    except (OSError, RuntimeError, ValueError) as error:
        return GitIgnoreCommandError(
            code="git_filesystem_inventory_path_mapping_error",
            message=f"could not map the selected filesystem root into the Git worktree: {error}",
            cwd=repository.root,
            command=("ls-files",),
        )
    return physical_repository_root, selected_prefix


def inventory_commands(selected_prefix: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        _inventory_arguments(("--cached", "--others", "--exclude-standard"), selected_prefix=selected_prefix),
        _inventory_arguments(("--others", "--ignored", "--exclude-standard"), selected_prefix=selected_prefix),
        _inventory_arguments(
            ("--others", "--ignored", "--exclude-standard", "--directory"),
            selected_prefix=selected_prefix,
        ),
    )


def normalized_inventory_path(raw_path: bytes) -> bytes:
    if not INVENTORY_PATHS_NEED_NORMALIZATION:
        return raw_path
    return b"/".join(os.fsencode(os.path.normcase(os.fsdecode(part))) for part in raw_path.split(b"/"))


def parse_inventory_output(output: bytes, *, selected_prefix: Path) -> tuple[tuple[str, ...], frozenset[str]]:
    if output and not output.endswith(b"\0"):
        raise ValueError("NUL-delimited Git output has no final terminator")
    raw_paths = output[:-1].split(b"\0") if output else ()
    prefix_parts = _inventory_prefix_parts(selected_prefix)
    paths: list[str] = []
    directory_hints: set[str] = set()
    seen: set[bytes] = set()
    for raw_path in raw_paths:
        selected_relative, directory_hint = _parse_inventory_record(raw_path, prefix_parts=prefix_parts)
        if selected_relative not in seen:
            decoded = os.fsdecode(selected_relative)
            paths.append(decoded)
            seen.add(selected_relative)
        else:
            decoded = os.fsdecode(selected_relative)
        if directory_hint:
            directory_hints.add(decoded)
    return tuple(paths), frozenset(directory_hints)


def merge_inventory_stderr(values: Sequence[bytes]) -> bytes:
    return b"\n".join(dict.fromkeys(line for output in values for line in output.splitlines() if line))[
        :MAX_INVENTORY_STDERR_BYTES
    ]


def inventory_stderr_error(root: Path, command: tuple[str, ...], stderr: bytes) -> GitIgnoreCommandError:
    bounded = stderr[:MAX_INVENTORY_STDERR_BYTES]
    lines = bounded.decode("utf-8", errors="surrogateescape").splitlines()
    detail = "; ".join(_without_git_severity_prefix(line) for line in lines)
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_incomplete",
        message=detail,
        cwd=root,
        command=command,
        stderr=bounded,
    )


def unavailable_error(repository: GitIgnoreRepository) -> GitIgnoreCommandError:
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_unavailable",
        message="a bare repository has no filesystem worktree inventory",
        cwd=repository.root,
        command=("ls-files",),
    )


def parse_error(root: Path, error: Exception) -> GitIgnoreCommandError:
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_parse_error",
        message=f"could not parse Git filesystem inventory: {error}",
        cwd=root,
        command=("ls-files",),
    )


def storage_error(root: Path, error: Exception) -> GitIgnoreCommandError:
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_storage_error",
        message=f"could not use temporary storage for the Git filesystem inventory: {error}",
        cwd=root,
        command=("ls-files",),
    )


def _inventory_arguments(options: tuple[str, ...], *, selected_prefix: Path) -> tuple[str, ...]:
    arguments = ("ls-files", "-z", "--full-name", *options)
    if selected_prefix == Path("."):
        return arguments
    pathspec = f":(top,literal){selected_prefix.as_posix()}"
    return (*arguments, "--", pathspec)


def _inventory_prefix_parts(selected_prefix: Path) -> tuple[bytes, ...] | None:
    if selected_prefix == Path("."):
        return None
    return tuple(os.fsencode(part) for part in PurePosixPath(selected_prefix.as_posix()).parts)


def _parse_inventory_record(raw_path: bytes, *, prefix_parts: tuple[bytes, ...] | None) -> tuple[bytes, bool]:
    if not raw_path:
        raise ValueError("Git returned an empty inventory path")
    directory_hint = raw_path.endswith(b"/")
    if directory_hint:
        raw_path = raw_path[:-1]
        if not raw_path:
            raise ValueError("Git returned an empty inventory directory")
    parts = raw_path.split(b"/")
    if raw_path.startswith(b"/") or any(part in {b"", b".", b".."} for part in parts):
        raise ValueError(f"Git returned an unsafe inventory path: {os.fsdecode(raw_path)!r}")
    if prefix_parts is None:
        return raw_path, directory_hint
    if len(parts) >= len(prefix_parts) and all(
        os.path.normcase(os.fsdecode(actual)) == os.path.normcase(os.fsdecode(expected))
        for actual, expected in zip(parts[: len(prefix_parts)], prefix_parts, strict=True)
    ):
        remainder = parts[len(prefix_parts) :]
        return (b"/".join(remainder) if remainder else b"."), directory_hint
    raise ValueError(f"Git returned a path outside the selected root: {os.fsdecode(raw_path)!r}")


def _without_git_severity_prefix(message: str) -> str:
    for prefix in ("warning: ", "error: ", "fatal: "):
        if message.casefold().startswith(prefix):
            return message[len(prefix) :]
    return message
