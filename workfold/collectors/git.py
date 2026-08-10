"""Safe local Git repository and commit collection."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic, CollectorResult
from workfold.collectors.git_objects import GitObjectParseError, ParsedCommit, parse_cat_file_batch, parse_commit_object
from workfold.config import RefScope
from workfold.coverage import DiagnosticSeverity
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_commit_id

_OID_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_LOCAL_READ_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "cat-file",
        "check-ignore",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "reflog",
        "rev-list",
        "rev-parse",
        "show-ref",
    }
)
_REPOSITORY_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
_REPOSITORY_ENVIRONMENT_PREFIXES: Final[tuple[str, ...]] = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)


class GitCommandError(RuntimeError):
    """Structured subprocess failure raised by :class:`GitRunner`."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        command: tuple[str, ...],
        cwd: Path,
        returncode: int | None = None,
        stderr: bytes = b"",
        stderr_truncated: bool = False,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.command = command
        self.cwd = cwd
        self.returncode = returncode
        self.stderr = stderr
        self.stderr_truncated = stderr_truncated
        self.hint = hint

    @property
    def stderr_text(self) -> str:
        """Decode diagnostic stderr without discarding invalid bytes."""

        text = self.stderr.decode("utf-8", errors="surrogateescape").rstrip()
        return f"{text}…" if self.stderr_truncated else text


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitRunner:
    """Invoke an allow-listed set of read-only local Git plumbing commands."""

    def __init__(
        self,
        executable: str = "git",
        *,
        process_runner: ProcessRunner = subprocess.run,
        stderr_limit: int = 16_384,
        timeout: float | None = None,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        if stderr_limit < 0:
            raise ValueError("stderr_limit must be non-negative")
        self._executable = executable
        self._process_runner = process_runner
        self._stderr_limit = stderr_limit
        self._timeout = timeout
        self._base_environment = dict(os.environ if base_environment is None else base_environment)

    def _environment(self) -> dict[str, str]:
        environment = dict(self._base_environment)
        for name in tuple(environment):
            if name in _REPOSITORY_ENVIRONMENT or name.startswith(_REPOSITORY_ENVIRONMENT_PREFIXES):
                environment.pop(name)
        environment.update(
            {
                "GIT_ASKPASS": "",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_EXTERNAL_DIFF": "",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
                "PAGER": "cat",
            }
        )
        return environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        allowed_returncodes: Collection[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one local command with prompts, pagers, protocols and lazy fetch disabled."""

        if not arguments or arguments[0] not in _LOCAL_READ_COMMANDS:
            command_name = arguments[0] if arguments else "<empty>"
            raise GitCommandError(
                code="unsafe_git_command",
                message=f"Git command is not allowed for local collection: {command_name}",
                command=tuple(arguments),
                cwd=cwd,
                hint="Workfold only invokes allow-listed, read-only Git commands.",
            )
        if any("\0" in argument for argument in arguments):
            raise GitCommandError(
                code="unsafe_git_argument",
                message="Git command argument contains a NUL byte",
                command=tuple(arguments),
                cwd=cwd,
            )

        command = (
            self._executable,
            "--no-pager",
            "-c",
            "color.ui=false",
            "-c",
            "core.pager=cat",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            *arguments,
        )
        try:
            completed = self._process_runner(
                command,
                cwd=os.fspath(cwd),
                env=self._environment(),
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=self._timeout,
            )
        except FileNotFoundError as error:
            raise GitCommandError(
                code="git_not_found",
                message=f"Git executable was not found: {self._executable}",
                command=command,
                cwd=cwd,
                hint="Install Git or use --mode fs.",
            ) from error
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            bounded, truncated = self._bounded_stderr(stderr)
            raise GitCommandError(
                code="git_command_timeout",
                message="Git command exceeded the configured timeout",
                command=command,
                cwd=cwd,
                stderr=bounded,
                stderr_truncated=truncated,
            ) from error
        except OSError as error:
            raise GitCommandError(
                code="git_spawn_error",
                message=f"Git command could not be started: {error}",
                command=command,
                cwd=cwd,
            ) from error

        if completed.returncode not in allowed_returncodes:
            bounded, truncated = self._bounded_stderr(completed.stderr)
            raise GitCommandError(
                code="git_command_failed",
                message=f"Git command failed with exit status {completed.returncode}",
                command=command,
                cwd=cwd,
                returncode=completed.returncode,
                stderr=bounded,
                stderr_truncated=truncated,
            )
        return completed

    def _bounded_stderr(self, stderr: bytes) -> tuple[bytes, bool]:
        if len(stderr) <= self._stderr_limit:
            return stderr, False
        return stderr[: self._stderr_limit], True


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
    """Unique repositories resolved from an exact set of requested paths.

    ``successful_targets`` counts input paths, while ``repositories`` contains
    each worktree context only once. Keeping both quantities prevents a
    duplicate path from being mistaken for either a failure or an additional
    context. Linked worktrees are retained because their local ``HEAD`` and
    other per-worktree reflogs are independent.
    """

    repositories: tuple[GitRepository, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_targets: int
    successful_targets: int
    duplicate_targets: int

    @property
    def is_partial(self) -> bool:
        """Whether at least one selected path could not be resolved."""

        return self.successful_targets != self.requested_targets


@dataclass(frozen=True, slots=True)
class CollectedGitCommit:
    """A raw parsed commit paired with its containing repository."""

    repository: GitRepository
    commit: ParsedCommit

    def to_origin(self) -> RecordOrigin:
        """Convert the parsed object to the shared provenance model."""

        return RecordOrigin(
            record_id=git_commit_id(self.repository.root, self.commit.object_id),
            source=Source.GIT,
            record_kind=RecordKind.COMMIT,
            repository_or_root=self.repository.root,
            commit_id=self.commit.object_id,
            object_id=self.commit.object_id,
            author_name=self.commit.author.identity.name,
            author_email=self.commit.author.identity.email,
            description=self.commit.subject,
        )

    def to_observation(self, kind: TimestampKind) -> TimestampObservation:
        """Convert one exact author/committer slot to a normalized observation."""

        if kind is TimestampKind.GIT_AUTHOR:
            signature = self.commit.author
        elif kind is TimestampKind.GIT_COMMITTER:
            signature = self.commit.committer
        else:
            raise ValueError("commit records support only Git author and committer timestamps")
        return TimestampObservation.create(
            self.to_origin(),
            kind,
            signature.epoch_nanoseconds,
            signature.raw_timestamp,
            original_offset_minutes=signature.offset_seconds // 60,
            actor_name=signature.identity.name,
            actor_email=signature.identity.email,
        )


@dataclass(frozen=True, slots=True)
class GitCommitRepositoryAccounting:
    """Reconciled commit collection counters for one resolved repository."""

    repository: GitRepository
    discovered_commit_ids: int
    captured_commits: int
    record_errors: int
    duplicate_commit_ids: int
    unavailable_objects: int
    parse_errors: int
    operational_errors: int
    successful: bool

    def __post_init__(self) -> None:
        counters = (
            self.discovered_commit_ids,
            self.captured_commits,
            self.record_errors,
            self.duplicate_commit_ids,
            self.unavailable_objects,
            self.parse_errors,
            self.operational_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git commit repository counters must be non-negative")
        if self.discovered_commit_ids != self.captured_commits + self.record_errors:
            raise ValueError("Git commit repository record accounting does not reconcile")

    @property
    def repository_root(self) -> Path:
        """Filesystem root used as the repository coverage target."""

        return self.repository.root

    @property
    def repository_identity(self) -> str:
        """Canonical repository identity used for collection deduplication."""

        return self.repository.identity

    @property
    def eligible_commits(self) -> int:
        """Commit records eligible for timestamp extraction."""

        return self.captured_commits


@dataclass(frozen=True, slots=True)
class GitCollectionResult:
    """Quick-view Git collection plus accounting needed by the shared ledger."""

    repositories: tuple[GitRepository, ...]
    commits: tuple[CollectedGitCommit, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_targets: int
    successful_repositories: int
    discovered_commit_ids: int
    duplicate_commit_ids: int
    unavailable_objects: int
    parse_errors: int
    repository_accounting: tuple[GitCommitRepositoryAccounting, ...] = ()
    duplicate_targets: int = 0

    @property
    def is_partial(self) -> bool:
        """Whether at least one requested operation failed."""

        return bool(self.diagnostics)

    def to_domain_result(
        self,
        timestamp_kinds: Sequence[TimestampKind] = (TimestampKind.GIT_AUTHOR,),
    ) -> CollectorResult[RecordOrigin, TimestampObservation]:
        """Adapt raw commits to the shared models without filtering their dates."""

        normalized_kinds = tuple(dict.fromkeys(timestamp_kinds))
        invalid = [
            kind for kind in normalized_kinds if kind not in {TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER}
        ]
        if invalid:
            names = ", ".join(kind.value for kind in invalid)
            raise ValueError(f"commit records do not expose timestamp kind(s): {names}")
        origins = tuple(item.to_origin() for item in self.commits)
        observations = tuple(item.to_observation(kind) for item in self.commits for kind in normalized_kinds)
        return CollectorResult(
            origins=origins,
            observations=observations,
            diagnostics=self.diagnostics,
        )


def _decode_path(value: bytes) -> Path:
    if not value.endswith(b"\n"):
        raise ValueError("Git path response has no record terminator")
    raw_path = value[:-1]
    if not raw_path or b"\0" in raw_path:
        raise ValueError("Git path response is empty or contains NUL")
    return Path(os.fsdecode(raw_path))


def _command_diagnostic(error: GitCommandError, *, stage: str, target: Path) -> CollectorDiagnostic:
    details = error.stderr_text
    message = str(error)
    if details:
        message = f"{message}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage=stage,
        target=os.fspath(target),
        path=os.fspath(target),
        message=message,
        hint=error.hint,
    )


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
                hint="Use --mode fs or pass a path inside a Git repository.",
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
        if is_bare:
            root = git_dir
        else:
            root = _resolve_git_output_path(
                runner.run(("rev-parse", "--show-toplevel"), cwd=probe).stdout,
                probe=probe,
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


def _resolve_git_output_path(payload: bytes, *, probe: Path) -> Path:
    path = _decode_path(payload)
    return (path if path.is_absolute() else probe / path).resolve()


def parse_commit_ids(output: bytes, *, repository: GitRepository) -> tuple[tuple[str, ...], int]:
    ids: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for raw_line in output.splitlines():
        try:
            object_id = raw_line.decode("ascii")
        except UnicodeDecodeError as error:
            raise GitCommandError(
                code="invalid_git_output",
                message="git rev-list returned a non-ASCII object ID",
                command=("rev-list",),
                cwd=repository.root,
            ) from error
        if not _OID_TEXT_RE.fullmatch(object_id):
            raise GitCommandError(
                code="invalid_git_output",
                message="git rev-list returned an invalid object ID",
                command=("rev-list",),
                cwd=repository.root,
            )
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
    """Enumerate reachable commits without applying timestamp traversal filters."""

    if ref_scope is RefScope.ALL_REFS:
        # `--all` includes HEAD and every locally present refs/* namespace.  It
        # performs no remote operation and deliberately has no date options.
        output = runner.run(("rev-list", "--all"), cwd=repository.root).stdout
    else:
        head = runner.run(
            ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
            cwd=repository.root,
            allowed_returncodes=(0, 1),
        )
        if ref_scope is RefScope.HEAD:
            if head.returncode == 1:
                return (), 0
            output = runner.run(("rev-list", "HEAD"), cwd=repository.root).stdout
        else:
            # Local branch refs capture work across branch switches without
            # fetched remote-tracking, tag, stash, or custom-ref-only history.
            # Include HEAD when it resolves so detached work is never hidden.
            revisions = ("rev-list", "--branches", "HEAD") if head.returncode == 0 else ("rev-list", "--branches")
            output = runner.run(revisions, cwd=repository.root).stdout
    return parse_commit_ids(output, repository=repository)


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


class GitRepositoryResolver:
    """Resolve selected paths to unique containing repositories only.

    This intentionally performs no ref or object traversal, allowing callers
    such as tag-only and reflog-only collection to remain independent of
    unrequested (and potentially corrupt) commit history.
    """

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._runner = runner or GitRunner()

    def resolve(self, paths: Sequence[Path]) -> GitRepositoryResolutionResult:
        """Resolve every path independently and retain target-level accounting."""

        diagnostics: list[CollectorDiagnostic] = []
        repositories: list[GitRepository] = []
        seen_repositories: set[str] = set()
        successful_targets = 0
        duplicate_targets = 0

        for path in paths:
            try:
                repository = resolve_repository(path, self._runner)
            except GitCommandError as error:
                diagnostics.append(_command_diagnostic(error, stage="git_repository_resolution", target=path))
                continue
            successful_targets += 1
            if repository.context_identity in seen_repositories:
                duplicate_targets += 1
                continue
            seen_repositories.add(repository.context_identity)
            repositories.append(repository)

        return GitRepositoryResolutionResult(
            repositories=tuple(repositories),
            diagnostics=tuple(diagnostics),
            requested_targets=len(paths),
            successful_targets=successful_targets,
            duplicate_targets=duplicate_targets,
        )


class GitCollector:
    """Collect unique raw commit records from one or more selected paths."""

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._runner = runner or GitRunner()

    def collect(self, paths: Sequence[Path], *, ref_scope: RefScope = RefScope.ALL_REFS) -> GitCollectionResult:
        """Collect whole repositories, continuing across independent target failures."""

        resolution = GitRepositoryResolver(self._runner).resolve(paths)
        diagnostics = list(resolution.diagnostics)
        repositories = list(resolution.repositories)

        commits: list[CollectedGitCommit] = []
        repository_accounting: list[GitCommitRepositoryAccounting] = []

        for repository in unique_semantic_repositories(repositories):
            diagnostic_start = len(diagnostics)
            discovered_for_repository = 0
            captured_for_repository = 0
            duplicates_for_repository = 0
            unavailable_for_repository = 0
            parse_errors_for_repository = 0
            successful_for_repository = False
            try:
                try:
                    object_ids, duplicates_for_repository = enumerate_commit_ids(
                        repository,
                        self._runner,
                        ref_scope,
                    )
                except GitCommandError as error:
                    diagnostics.append(_command_diagnostic(error, stage="git_commit_discovery", target=repository.root))
                    continue
                discovered_for_repository = len(object_ids)
                if not object_ids:
                    successful_for_repository = True
                    continue

                input_data = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
                try:
                    batch_output = self._runner.run(
                        ("cat-file", "--batch"),
                        cwd=repository.root,
                        input_data=input_data,
                    ).stdout
                    batch = parse_cat_file_batch(batch_output, object_ids)
                except GitCommandError as error:
                    diagnostics.append(_command_diagnostic(error, stage="git_object_read", target=repository.root))
                    continue
                except GitObjectParseError as error:
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_object_read",
                            target=os.fspath(repository.root),
                            provenance_id=error.object_id,
                            message=str(error),
                            hint="The repository may be corrupt or may have changed during collection.",
                        )
                    )
                    # A malformed batch envelope prevents assigning any response
                    # to a requested object safely, so every object in this batch
                    # has an accounted parse failure.
                    parse_errors_for_repository += len(object_ids)
                    continue

                for unavailable in batch.unavailable:
                    unavailable_for_repository += 1
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_object_unavailable",
                            stage="git_object_read",
                            target=os.fspath(repository.root),
                            provenance_id=unavailable.requested_id,
                            message=f"Git object is unavailable ({unavailable.reason})",
                            hint="The repository may be shallow or partial; Workfold will not fetch missing objects.",
                        )
                    )
                for batch_object in batch.objects:
                    if batch_object.object_type != "commit":
                        parse_errors_for_repository += 1
                        diagnostics.append(
                            CollectorDiagnostic(
                                code="git_object_not_commit",
                                stage="git_object_parse",
                                target=os.fspath(repository.root),
                                provenance_id=batch_object.object_id,
                                message=f"rev-list object has unexpected type {batch_object.object_type!r}",
                            )
                        )
                        continue
                    try:
                        parsed = parse_commit_object(batch_object.object_id, batch_object.data)
                    except GitObjectParseError as error:
                        parse_errors_for_repository += 1
                        diagnostics.append(
                            CollectorDiagnostic(
                                code=error.code,
                                stage="git_object_parse",
                                target=os.fspath(repository.root),
                                provenance_id=error.object_id,
                                message=str(error),
                                hint="The commit object is malformed and was not plotted.",
                            )
                        )
                        continue
                    commits.append(CollectedGitCommit(repository=repository, commit=parsed))
                    captured_for_repository += 1
                successful_for_repository = True
            finally:
                operational_errors = sum(
                    item.severity is DiagnosticSeverity.ERROR for item in diagnostics[diagnostic_start:]
                )
                repository_accounting.append(
                    GitCommitRepositoryAccounting(
                        repository=repository,
                        discovered_commit_ids=discovered_for_repository,
                        captured_commits=captured_for_repository,
                        record_errors=discovered_for_repository - captured_for_repository,
                        duplicate_commit_ids=duplicates_for_repository,
                        unavailable_objects=unavailable_for_repository,
                        parse_errors=parse_errors_for_repository,
                        operational_errors=operational_errors,
                        successful=successful_for_repository,
                    )
                )

        successful_count = sum(item.successful for item in repository_accounting)
        discovered_count = sum(item.discovered_commit_ids for item in repository_accounting)
        duplicate_count = sum(item.duplicate_commit_ids for item in repository_accounting)
        unavailable_count = sum(item.unavailable_objects for item in repository_accounting)
        parse_error_count = sum(item.parse_errors for item in repository_accounting)

        return GitCollectionResult(
            repositories=tuple(repositories),
            commits=tuple(commits),
            diagnostics=tuple(diagnostics),
            requested_targets=len(paths),
            successful_repositories=successful_count,
            discovered_commit_ids=discovered_count,
            duplicate_commit_ids=duplicate_count,
            unavailable_objects=unavailable_count,
            parse_errors=parse_error_count,
            repository_accounting=tuple(repository_accounting),
            duplicate_targets=resolution.duplicate_targets,
        )


__all__ = [
    "CollectedGitCommit",
    "GitCommitRepositoryAccounting",
    "GitCollectionResult",
    "GitCollector",
    "GitCommandError",
    "GitRepository",
    "GitRepositoryResolutionResult",
    "GitRepositoryResolver",
    "GitRunner",
    "enumerate_commit_ids",
    "parse_commit_ids",
    "resolve_repository",
    "unique_semantic_repositories",
]
