"""Filesystem exclusion patterns and standard Git ignore integration."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from pathspec import GitIgnoreSpec

from workfold.provenance import lexical_absolute

_GIT_SAFETY_OPTIONS: Final[tuple[str, ...]] = (
    "--no-pager",
    "-c",
    "color.ui=false",
    "-c",
    "core.pager=cat",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.allow=never",
)
_GIT_IGNORE_COMMANDS: Final[frozenset[str]] = frozenset({"check-ignore", "ls-files", "rev-parse"})
_MAX_GITDIR_POINTER_BYTES: Final[int] = 4096
_MAX_INVENTORY_STDERR_BYTES: Final[int] = 16_384


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
class ExplicitExcluder:
    """Match the non-negating Git-wildmatch subset used by ``--exclude``."""

    patterns: tuple[str, ...]
    _spec: GitIgnoreSpec

    @classmethod
    def compile(cls, patterns: Sequence[str]) -> ExplicitExcluder:
        """Validate and compile repeatable exclusion patterns."""

        normalized = tuple(patterns)
        for pattern in normalized:
            if not pattern:
                raise ExclusionPatternError("explicit exclusion patterns cannot be empty")
            if pattern.startswith("!"):
                raise ExclusionPatternError(f"negated --exclude patterns are not supported: {pattern!r}")
            if "\0" in pattern:
                raise ExclusionPatternError("explicit exclusion patterns cannot contain NUL bytes")
        return cls(normalized, GitIgnoreSpec.from_lines(normalized))

    def matches(self, relative_path: PurePosixPath | str, *, is_directory: bool) -> bool:
        """Return whether a root-relative path is explicitly excluded."""

        value = relative_path.as_posix() if isinstance(relative_path, PurePosixPath) else relative_path
        value = value.lstrip("/")
        if not value or value == ".":
            return False
        if is_directory and not value.endswith("/"):
            value += "/"
        return self._spec.match_file(value)


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
    """Git-authoritative path candidates for one current worktree scope.

    Git decides only whether a leaf path is tracked, untracked, or ignored.
    Callers must still use a no-follow filesystem stat before treating an
    included candidate as a current filesystem entry.
    """

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


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitIgnoreRunner:
    """Run only local Git commands needed for standard ignore evaluation.

    Unlike the history collector, this runner deliberately retains system and
    user Git configuration because ``core.excludesFile`` is part of the
    required ignore semantics.
    """

    def __init__(
        self,
        executable: str = "git",
        *,
        process_runner: ProcessRunner = subprocess.run,
        base_environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
        stderr_limit: int = 16_384,
    ) -> None:
        if stderr_limit < 0:
            raise ValueError("stderr_limit must be non-negative")
        self._executable = executable
        self._process_runner = process_runner
        self._base_environment = dict(os.environ if base_environment is None else base_environment)
        self._timeout = timeout
        self._stderr_limit = stderr_limit

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        allowed_returncodes: Collection[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a validated read-only Git ignore command without a shell."""

        if not arguments or arguments[0] not in _GIT_IGNORE_COMMANDS:
            raise GitIgnoreCommandError(
                code="unsafe_git_ignore_command",
                message="only rev-parse, check-ignore, and ls-files are allowed for filesystem ignores",
                cwd=cwd,
                command=tuple(arguments),
            )
        if any("\0" in argument for argument in arguments):
            raise GitIgnoreCommandError(
                code="unsafe_git_ignore_argument",
                message="Git ignore command arguments cannot contain NUL bytes",
                cwd=cwd,
                command=tuple(arguments),
            )
        command = (self._executable, *_GIT_SAFETY_OPTIONS, *arguments)
        environment = dict(self._base_environment)
        for name in tuple(environment):
            if (
                name
                in {
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_CEILING_DIRECTORIES",
                    "GIT_COMMON_DIR",
                    "GIT_CONFIG",
                    "GIT_CONFIG_COUNT",
                    "GIT_CONFIG_PARAMETERS",
                    "GIT_DIR",
                    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                    "GIT_INDEX_FILE",
                    "GIT_NAMESPACE",
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_PREFIX",
                    "GIT_WORK_TREE",
                }
                or name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")
                or name.startswith("GIT_TRACE")
            ):
                environment.pop(name, None)
        environment.update(
            {
                "GIT_ASKPASS": "",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
                "PAGER": "cat",
            }
        )
        try:
            completed = self._process_runner(
                command,
                cwd=os.fspath(cwd),
                env=environment,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=self._timeout,
            )
        except FileNotFoundError as error:
            raise GitIgnoreCommandError(
                code="git_not_found_for_ignores",
                message=f"Git executable was not found: {self._executable}",
                cwd=cwd,
                command=command,
                unavailable=True,
            ) from error
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            raise GitIgnoreCommandError(
                code="git_ignore_timeout",
                message="Git ignore evaluation exceeded its timeout",
                cwd=cwd,
                command=command,
                stderr=stderr[: self._stderr_limit],
            ) from error
        except OSError as error:
            raise GitIgnoreCommandError(
                code="git_ignore_spawn_error",
                message=f"Git ignore evaluation could not start: {error}",
                cwd=cwd,
                command=command,
            ) from error
        if completed.returncode not in allowed_returncodes:
            raise GitIgnoreCommandError(
                code="git_ignore_command_failed",
                message=f"Git ignore command failed with exit status {completed.returncode}",
                cwd=cwd,
                command=command,
                returncode=completed.returncode,
                stderr=completed.stderr[: self._stderr_limit],
            )
        return completed


class GitIgnoreService:
    """Discover worktrees and ask Git for its authoritative ignore decisions."""

    def __init__(self, runner: GitIgnoreRunner | None = None) -> None:
        self._runner = runner or GitIgnoreRunner()

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
            admin_root = _parse_absolute_git_path(admin_result.stdout, field="administrative directory")
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
            root = _parse_absolute_git_path(top_level.stdout, field="worktree root")
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
                physical_lexical_root = _physical_path_without_following_final(lexical_root)
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
                    physical_path = _physical_path_without_following_final(candidate.path)
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

        if repository.is_bare:
            return GitFilesystemInventory(
                error=GitIgnoreCommandError(
                    code="git_filesystem_inventory_unavailable",
                    message="a bare repository has no filesystem worktree inventory",
                    cwd=repository.root,
                    command=("ls-files",),
                )
            )
        try:
            physical_repository_root = repository.root.resolve(strict=True)
            physical_selected_root = selected_root.resolve(strict=True)
            selected_prefix = physical_selected_root.relative_to(physical_repository_root)
        except (OSError, RuntimeError, ValueError) as error:
            return GitFilesystemInventory(
                error=GitIgnoreCommandError(
                    code="git_filesystem_inventory_path_mapping_error",
                    message=f"could not map the selected filesystem root into the Git worktree: {error}",
                    cwd=repository.root,
                    command=("ls-files",),
                )
            )

        included_arguments = _inventory_arguments(
            ("--cached", "--others", "--exclude-standard"),
            selected_prefix=selected_prefix,
        )
        ignored_arguments = _inventory_arguments(
            ("--others", "--ignored", "--exclude-standard"),
            selected_prefix=selected_prefix,
        )
        ignored_directory_arguments = _inventory_arguments(
            ("--others", "--ignored", "--exclude-standard", "--directory"),
            selected_prefix=selected_prefix,
        )
        try:
            included_result = self._runner.run(included_arguments, cwd=physical_repository_root)
            ignored_result = self._runner.run(ignored_arguments, cwd=physical_repository_root)
            ignored_directory_result = self._runner.run(
                ignored_directory_arguments,
                cwd=physical_repository_root,
            )
            included, _ = _parse_inventory_output(
                included_result.stdout,
                selected_prefix=selected_prefix,
            )
            ignored, ignored_directories = _parse_inventory_output(
                ignored_result.stdout,
                selected_prefix=selected_prefix,
            )
            _, ignored_directory_boundaries = _parse_inventory_output(
                ignored_directory_result.stdout,
                selected_prefix=selected_prefix,
            )
            stderr = b"\n".join(
                dict.fromkeys(
                    line
                    for output in (
                        included_result.stderr,
                        ignored_result.stderr,
                        ignored_directory_result.stderr,
                    )
                    for line in output.splitlines()
                    if line
                )
            )
            warning = _inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
            return GitFilesystemInventory(
                included,
                ignored,
                ignored_directories | ignored_directory_boundaries,
                warning=warning,
            )
        except GitIgnoreCommandError as error:
            return GitFilesystemInventory(error=error)
        except ValueError as error:
            return GitFilesystemInventory(
                error=GitIgnoreCommandError(
                    code="git_filesystem_inventory_parse_error",
                    message=f"could not parse Git filesystem inventory: {error}",
                    cwd=physical_repository_root,
                    command=("ls-files",),
                )
            )


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
        physical_path = _physical_path_without_following_final(path)
    except (OSError, RuntimeError):
        return False
    if _is_same_or_descendant(physical_path, admin_root):
        return True
    if is_git_admin_name(path):
        try:
            final_target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return _is_same_or_descendant(final_target, admin_root)
    return False


def has_git_admin_ancestor(path: Path) -> bool:
    """Detect lexical or physical Git storage without following ``path`` itself."""

    if _has_plausible_admin_ancestor(path):
        return True
    try:
        physical_path = _physical_path_without_following_final(path)
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


def _looks_like_worktree_marker(path: Path) -> bool:
    """Recognize a plausible worktree marker without following symlinks.

    A regular ``.git`` file is the standard linked-worktree/submodule marker.
    A directory needs at least its mandatory ``HEAD`` file. Requiring that
    small amount of shape avoids treating unrelated empty ``.git`` directories
    above a selection as repositories when Git itself correctly rejects them.
    """

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
        value = os.read(descriptor, _MAX_GITDIR_POINTER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(value) > _MAX_GITDIR_POINTER_BYTES or not value.startswith(b"gitdir: "):
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


def _physical_path_without_following_final(path: Path) -> Path:
    """Resolve ancestors for evaluation while retaining the final directory entry."""

    return path.parent.resolve(strict=True) / path.name


def _inventory_arguments(options: tuple[str, ...], *, selected_prefix: Path) -> tuple[str, ...]:
    arguments = ("ls-files", "-z", "--full-name", *options)
    if selected_prefix == Path("."):
        return arguments
    pathspec = f":(top,literal){selected_prefix.as_posix()}"
    return (*arguments, "--", pathspec)


def _parse_inventory_output(
    output: bytes,
    *,
    selected_prefix: Path,
) -> tuple[tuple[str, ...], frozenset[str]]:
    if output and not output.endswith(b"\0"):
        raise ValueError("NUL-delimited Git output has no final terminator")
    raw_paths = output[:-1].split(b"\0") if output else ()
    prefix_parts = (
        None
        if selected_prefix == Path(".")
        else tuple(os.fsencode(part) for part in PurePosixPath(selected_prefix.as_posix()).parts)
    )
    paths: list[str] = []
    directory_hints: set[str] = set()
    seen: set[bytes] = set()
    for raw_path in raw_paths:
        if not raw_path:
            raise ValueError("Git returned an empty inventory path")
        # Git represents an untracked nested repository as one directory
        # boundary even without ``--directory``. It is still one candidate;
        # the collector's lstat decides its actual current type.
        directory_hint = raw_path.endswith(b"/")
        if directory_hint:
            raw_path = raw_path[:-1]
            if not raw_path:
                raise ValueError("Git returned an empty inventory directory")
        parts = raw_path.split(b"/")
        if raw_path.startswith(b"/") or any(part in {b"", b".", b".."} for part in parts):
            raise ValueError(f"Git returned an unsafe inventory path: {os.fsdecode(raw_path)!r}")
        if prefix_parts is None:
            selected_relative = raw_path
        elif len(parts) >= len(prefix_parts) and all(
            os.path.normcase(os.fsdecode(actual)) == os.path.normcase(os.fsdecode(expected))
            for actual, expected in zip(parts[: len(prefix_parts)], prefix_parts, strict=True)
        ):
            remainder = parts[len(prefix_parts) :]
            selected_relative = b"/".join(remainder) if remainder else b"."
        else:
            raise ValueError(f"Git returned a path outside the selected root: {os.fsdecode(raw_path)!r}")
        if selected_relative not in seen:
            decoded = os.fsdecode(selected_relative)
            paths.append(decoded)
            seen.add(selected_relative)
        else:
            decoded = os.fsdecode(selected_relative)
        if directory_hint:
            directory_hints.add(decoded)
    return tuple(paths), frozenset(directory_hints)


def _inventory_stderr_error(root: Path, command: tuple[str, ...], stderr: bytes) -> GitIgnoreCommandError:
    bounded = stderr[:_MAX_INVENTORY_STDERR_BYTES]
    detail = bounded.decode("utf-8", errors="surrogateescape").rstrip()
    return GitIgnoreCommandError(
        code="git_filesystem_inventory_incomplete",
        message=f"Git reported an incomplete filesystem inventory: {detail}",
        cwd=root,
        command=command,
        stderr=bounded,
    )


def _is_same_or_descendant(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parse_absolute_git_path(value: bytes, *, field: str) -> Path:
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


def _has_plausible_admin_ancestor(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if is_git_admin_path(candidate) or looks_like_bare_repository(candidate):
            return True
    return False


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


__all__ = [
    "ExclusionPatternError",
    "ExplicitExcluder",
    "GitFilesystemInventory",
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
