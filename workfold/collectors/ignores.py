"""Filesystem exclusion patterns and standard Git ignore integration."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import tempfile
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
_INVENTORY_PATHS_NEED_NORMALIZATION: Final[bool] = os.path.normcase("A/B") != "A/B"


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

        command = self._command(arguments, cwd=cwd)
        environment = self._environment()
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

    def consume_stdout(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        consumer: Callable[[bytes], None],
        allowed_returncodes: Collection[int] = (0,),
    ) -> bytes:
        """Feed bounded stdout chunks to *consumer* and return bounded stderr.

        The production path never retains the complete Git response. Injected
        runners, subclasses, and timeout-enabled runners keep using ``run`` so
        tests and custom integrations preserve their interception semantics.
        """

        if (
            self._process_runner is not subprocess.run
            or type(self).run is not GitIgnoreRunner.run
            or self._timeout is not None
        ):
            completed = self.run(arguments, cwd=cwd, allowed_returncodes=allowed_returncodes)
            consumer(completed.stdout)
            return completed.stderr[: self._stderr_limit]

        command = self._command(arguments, cwd=cwd)
        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=os.fspath(cwd),
                    env=self._environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    shell=False,
                )
            except FileNotFoundError as error:
                raise GitIgnoreCommandError(
                    code="git_not_found_for_ignores",
                    message=f"Git executable was not found: {self._executable}",
                    cwd=cwd,
                    command=command,
                    unavailable=True,
                ) from error
            except OSError as error:
                raise GitIgnoreCommandError(
                    code="git_ignore_spawn_error",
                    message=f"Git ignore evaluation could not start: {error}",
                    cwd=cwd,
                    command=command,
                ) from error

            completed = False
            try:
                if process.stdout is None:
                    raise RuntimeError("streaming Git ignore process omitted stdout")
                while chunk := process.stdout.read(64 * 1024):
                    consumer(chunk)
                process.stdout.close()
                returncode = process.wait()
                completed = True
            finally:
                if not completed and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            stderr_file.seek(0)
            stderr = stderr_file.read(self._stderr_limit + 1)
            if returncode not in allowed_returncodes:
                raise GitIgnoreCommandError(
                    code="git_ignore_command_failed",
                    message=f"Git ignore command failed with exit status {returncode}",
                    cwd=cwd,
                    command=command,
                    returncode=returncode,
                    stderr=stderr[: self._stderr_limit],
                )
            return stderr[: self._stderr_limit]

    def _command(self, arguments: Sequence[str], *, cwd: Path) -> tuple[str, ...]:
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
        return (self._executable, *_GIT_SAFETY_OPTIONS, *arguments)

    def _environment(self) -> dict[str, str]:
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
        return environment


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
            stderr = _merge_inventory_stderr(
                (included_result.stderr, ignored_result.stderr, ignored_directory_result.stderr)
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
        Subclasses overriding :meth:`inventory` retain their interception
        behavior through the materialized compatibility path.
        """

        if type(self).inventory is not GitIgnoreService.inventory:
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

        if repository.is_bare:
            return GitFilesystemInventoryVisit(
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
            return GitFilesystemInventoryVisit(
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

        stderr_values: list[bytes] = []
        delivering_callbacks = False
        try:
            with tempfile.TemporaryDirectory(prefix="workfold-ignore-inventory-") as directory:
                connection = sqlite3.connect(f"{directory}/inventory.sqlite3")
                try:
                    connection.execute("PRAGMA journal_mode=OFF")
                    connection.execute("PRAGMA synchronous=OFF")
                    connection.execute("PRAGMA temp_store=FILE")
                    connection.execute("PRAGMA cache_size=-2048")
                    connection.execute(
                        """
                        CREATE TABLE inventory (
                            ordinal INTEGER PRIMARY KEY,
                            path BLOB NOT NULL,
                            normalized_path BLOB NOT NULL UNIQUE,
                            category INTEGER NOT NULL CHECK (category IN (0, 1))
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE ignored_directories (
                            path BLOB PRIMARY KEY,
                            normalized_path BLOB NOT NULL UNIQUE
                        )
                        """
                    )
                    ordinal = 0

                    def insert_path(category: int) -> Callable[[bytes, bool], None]:
                        def insert(raw_path: bytes, directory_hint: bool) -> None:
                            nonlocal ordinal
                            normalized_path = _normalized_inventory_path(raw_path)
                            inserted = connection.execute(
                                "INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, ?)",
                                (ordinal, raw_path, normalized_path, category),
                            ).rowcount
                            if inserted:
                                ordinal += 1
                            else:
                                existing = connection.execute(
                                    "SELECT path, category FROM inventory WHERE normalized_path = ?",
                                    (normalized_path,),
                                ).fetchone()
                                if existing != (raw_path, category):
                                    raise ValueError(
                                        f"Git filesystem inventory contains a duplicate or overlapping path: "
                                        f"{os.fsdecode(raw_path)!r}"
                                    )
                            if category == 1 and directory_hint:
                                connection.execute(
                                    "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                                    (raw_path, normalized_path),
                                )

                        return insert

                    def insert_directory(raw_path: bytes, _directory_hint: bool) -> None:
                        connection.execute(
                            "INSERT OR IGNORE INTO ignored_directories VALUES (?, ?)",
                            (raw_path, _normalized_inventory_path(raw_path)),
                        )

                    for arguments, consumer in (
                        (included_arguments, insert_path(0)),
                        (ignored_arguments, insert_path(1)),
                        (ignored_directory_arguments, insert_directory),
                    ):
                        decoder = _InventoryStreamDecoder(selected_prefix, consumer)
                        stderr_values.append(
                            self._runner.consume_stdout(
                                arguments,
                                cwd=physical_repository_root,
                                consumer=decoder.feed,
                            )
                        )
                        decoder.finish()
                    _validate_inventory_relationships(connection)
                    connection.commit()

                    included_count = connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 0").fetchone()[
                        0
                    ]
                    ignored_count = connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 1").fetchone()[
                        0
                    ]
                    delivering_callbacks = True
                    for (raw_path,) in connection.execute(
                        "SELECT path FROM inventory WHERE category = 0 ORDER BY ordinal"
                    ):
                        included_consumer(os.fsdecode(raw_path))
                    for raw_path, is_directory in connection.execute(
                        """
                        SELECT inventory.path, ignored_directories.path IS NOT NULL
                          FROM inventory
                          LEFT JOIN ignored_directories USING (path)
                         WHERE category = 1
                         ORDER BY ordinal
                        """
                    ):
                        ignored_consumer(os.fsdecode(raw_path), bool(is_directory))
                finally:
                    connection.close()
        except GitIgnoreCommandError as error:
            return GitFilesystemInventoryVisit(error=error)
        except (OSError, sqlite3.Error, ValueError) as error:
            if delivering_callbacks:
                raise
            return GitFilesystemInventoryVisit(
                error=GitIgnoreCommandError(
                    code="git_filesystem_inventory_parse_error",
                    message=f"could not parse Git filesystem inventory: {error}",
                    cwd=physical_repository_root,
                    command=("ls-files",),
                )
            )

        stderr = _merge_inventory_stderr(stderr_values)
        warning = _inventory_stderr_error(physical_repository_root, ("ls-files",), stderr) if stderr else None
        return GitFilesystemInventoryVisit(
            included_paths=int(included_count),
            ignored_paths=int(ignored_count),
            warning=warning,
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


def _normalized_inventory_path(raw_path: bytes) -> bytes:
    """Return a separator-stable platform-normalized inventory key."""

    if not _INVENTORY_PATHS_NEED_NORMALIZATION:
        return raw_path
    return b"/".join(os.fsencode(os.path.normcase(os.fsdecode(part))) for part in raw_path.split(b"/"))


def _validate_inventory_relationships(connection: sqlite3.Connection) -> None:
    """Reject included leaves at or below a Git-reported ignored directory."""

    included_count = int(connection.execute("SELECT COUNT(*) FROM inventory WHERE category = 0").fetchone()[0])
    directory_count = int(connection.execute("SELECT COUNT(*) FROM ignored_directories").fetchone()[0])
    conflict: tuple[bytes] | None = None
    if directory_count <= included_count:
        for (directory,) in connection.execute("SELECT normalized_path FROM ignored_directories"):
            prefix = directory.rstrip(b"/") + b"/"
            upper_bound = directory.rstrip(b"/") + b"0"
            conflict = connection.execute(
                """
                SELECT path FROM inventory
                 WHERE category = 0
                   AND (normalized_path = ? OR (normalized_path >= ? AND normalized_path < ?))
                 LIMIT 1
                """,
                (directory, prefix, upper_bound),
            ).fetchone()
            if conflict is not None:
                break
    else:
        for raw_path, normalized_path in connection.execute(
            "SELECT path, normalized_path FROM inventory WHERE category = 0"
        ):
            parts = normalized_path.split(b"/")
            for size in range(1, len(parts) + 1):
                candidate = b"/".join(parts[:size])
                if (
                    connection.execute(
                        "SELECT 1 FROM ignored_directories WHERE normalized_path = ?",
                        (candidate,),
                    ).fetchone()
                    is not None
                ):
                    conflict = (raw_path,)
                    break
            if conflict is not None:
                break
    if conflict is not None:
        raise ValueError(
            f"Git filesystem inventory places an included path below an ignored directory: {os.fsdecode(conflict[0])!r}"
        )


def _parse_inventory_output(
    output: bytes,
    *,
    selected_prefix: Path,
) -> tuple[tuple[str, ...], frozenset[str]]:
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


class _InventoryStreamDecoder:
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


def _inventory_prefix_parts(selected_prefix: Path) -> tuple[bytes, ...] | None:
    if selected_prefix == Path("."):
        return None
    return tuple(os.fsencode(part) for part in PurePosixPath(selected_prefix.as_posix()).parts)


def _parse_inventory_record(
    raw_path: bytes,
    *,
    prefix_parts: tuple[bytes, ...] | None,
) -> tuple[bytes, bool]:
    if not raw_path:
        raise ValueError("Git returned an empty inventory path")
    # Git represents an untracked nested repository as one directory boundary
    # even without ``--directory``. The collector's lstat remains authoritative
    # for the entry's current type.
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


def _merge_inventory_stderr(values: Sequence[bytes]) -> bytes:
    return b"\n".join(dict.fromkeys(line for output in values for line in output.splitlines() if line))[
        :_MAX_INVENTORY_STDERR_BYTES
    ]


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
