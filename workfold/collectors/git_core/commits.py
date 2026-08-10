"""Reachable Git commit enumeration, parsing, and accounting."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.git_core.repository import (
    GitRepository,
    GitRepositoryResolver,
    unique_semantic_repositories,
)
from workfold.collectors.git_core.runner import GitCommandError, GitRunner, command_diagnostic
from workfold.collectors.git_objects import GitObjectParseError, ParsedCommit, parse_cat_file_batch, parse_commit_object
from workfold.config import RefScope
from workfold.iterables import batched
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_commit_id

OID_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class CollectedGitCommit:
    """A raw parsed commit paired with its containing repository."""

    repository: GitRepository
    commit: ParsedCommit

    def to_origin(self) -> RecordOrigin:
        return RecordOrigin(
            record_id=git_commit_id(self.repository.root, self.commit.object_id),
            source=Source.GIT,
            record_kind=RecordKind.COMMIT,
            repository_or_root=self.repository.root,
            commit_id=self.commit.object_id,
            description=self.commit.subject,
        )

    def to_observation(self, kind: TimestampKind) -> TimestampObservation:
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
        return self.repository.root

    @property
    def repository_identity(self) -> str:
        return self.repository.identity

    @property
    def eligible_commits(self) -> int:
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
    records_retained: bool = True

    def __post_init__(self) -> None:
        captured = sum(item.captured_commits for item in self.repository_accounting)
        if self.repository_accounting:
            if self.records_retained and len(self.commits) != captured:
                raise ValueError("retained Git commits do not match repository accounting")
            if len(self.commits) > captured:
                raise ValueError("retained Git commits exceed captured repository accounting")

    @property
    def is_partial(self) -> bool:
        return bool(self.diagnostics)


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
        if not OID_TEXT_RE.fullmatch(object_id):
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
    if ref_scope is RefScope.ALL_REFS:
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
            revisions = ("rev-list", "--branches", "HEAD") if head.returncode == 0 else ("rev-list", "--branches")
            output = runner.run(revisions, cwd=repository.root).stdout
    return parse_commit_ids(output, repository=repository)


def iter_commit_ids(repository: GitRepository, runner: GitRunner, ref_scope: RefScope) -> Iterator[str]:
    if ref_scope is RefScope.ALL_REFS:
        revisions = ("rev-list", "--all")
    else:
        head = runner.run(
            ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
            cwd=repository.root,
            allowed_returncodes=(0, 1),
        )
        if ref_scope is RefScope.HEAD:
            if head.returncode == 1:
                return
            revisions = ("rev-list", "HEAD")
        else:
            revisions = ("rev-list", "--branches", "HEAD") if head.returncode == 0 else ("rev-list", "--branches")

    for raw_line in runner.iter_stdout_lines(revisions, cwd=repository.root):
        raw_object_id = raw_line.rstrip(b"\r\n")
        try:
            object_id = raw_object_id.decode("ascii")
        except UnicodeDecodeError as error:
            raise GitCommandError(
                code="invalid_git_output",
                message="git rev-list returned a non-ASCII object ID",
                command=revisions,
                cwd=repository.root,
            ) from error
        if not OID_TEXT_RE.fullmatch(object_id):
            raise GitCommandError(
                code="invalid_git_output",
                message="git rev-list returned an invalid object ID",
                command=revisions,
                cwd=repository.root,
            )
        yield object_id


class GitCollector:
    """Collect unique raw commit records from one or more selected paths."""

    def __init__(self, runner: GitRunner | None = None, *, object_batch_size: int = 2_048) -> None:
        if object_batch_size < 1:
            raise ValueError("object_batch_size must be positive")
        self._runner = runner or GitRunner()
        self._object_batch_size = object_batch_size

    def collect(
        self,
        paths: Sequence[Path],
        *,
        ref_scope: RefScope = RefScope.ALL_REFS,
        commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None = None,
        retain_commits: bool = True,
    ) -> GitCollectionResult:
        resolution = GitRepositoryResolver(self._runner).resolve(paths)
        diagnostics = DiagnosticBuffer()
        diagnostics.extend(resolution.diagnostics)
        repositories = list(resolution.repositories)
        commits: list[CollectedGitCommit] = []
        repository_accounting: list[GitCommitRepositoryAccounting] = []

        for repository in unique_semantic_repositories(repositories):
            accounting = _collect_repository_commits(
                repository,
                runner=self._runner,
                ref_scope=ref_scope,
                object_batch_size=self._object_batch_size,
                diagnostics=diagnostics,
                commits=commits,
                commit_consumer=commit_consumer,
                retain_commits=retain_commits,
            )
            repository_accounting.append(accounting)

        return GitCollectionResult(
            repositories=tuple(repositories),
            commits=tuple(commits),
            diagnostics=diagnostics.snapshot(),
            requested_targets=len(paths),
            successful_repositories=sum(item.successful for item in repository_accounting),
            discovered_commit_ids=sum(item.discovered_commit_ids for item in repository_accounting),
            duplicate_commit_ids=sum(item.duplicate_commit_ids for item in repository_accounting),
            unavailable_objects=sum(item.unavailable_objects for item in repository_accounting),
            parse_errors=sum(item.parse_errors for item in repository_accounting),
            repository_accounting=tuple(repository_accounting),
            duplicate_targets=resolution.duplicate_targets,
            records_retained=retain_commits,
        )


def _collect_repository_commits(
    repository: GitRepository,
    *,
    runner: GitRunner,
    ref_scope: RefScope,
    object_batch_size: int,
    diagnostics: DiagnosticBuffer,
    commits: list[CollectedGitCommit],
    commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None,
    retain_commits: bool,
) -> GitCommitRepositoryAccounting:
    error_count_start = diagnostics.error_count
    discovered = 0
    captured = 0
    unavailable_count = 0
    parse_error_count = 0
    successful = False
    object_read_failed = False
    try:
        try:
            for object_id_batch in batched(iter_commit_ids(repository, runner, ref_scope), object_batch_size):
                object_ids = tuple(object_id_batch)
                discovered += len(object_ids)
                input_data = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
                try:
                    batch_output = runner.run(("cat-file", "--batch"), cwd=repository.root, input_data=input_data).stdout
                    batch = parse_cat_file_batch(batch_output, object_ids)
                except GitCommandError as error:
                    diagnostics.append(command_diagnostic(error, stage="git_object_read", target=repository.root))
                    object_read_failed = True
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
                    parse_error_count += len(object_ids)
                    object_read_failed = True
                    continue

                for unavailable in batch.unavailable:
                    unavailable_count += 1
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

                captured_batch: list[CollectedGitCommit] = []
                for batch_object in batch.objects:
                    if batch_object.object_type != "commit":
                        parse_error_count += 1
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
                        parse_error_count += 1
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
                    collected = CollectedGitCommit(repository=repository, commit=parsed)
                    captured_batch.append(collected)
                    if retain_commits:
                        commits.append(collected)
                    captured += 1
                if captured_batch and commit_consumer is not None:
                    commit_consumer(tuple(captured_batch))
        except GitCommandError as error:
            diagnostics.append(command_diagnostic(error, stage="git_commit_discovery", target=repository.root))
        else:
            successful = not object_read_failed
    finally:
        operational_errors = diagnostics.error_count - error_count_start

    return GitCommitRepositoryAccounting(
        repository=repository,
        discovered_commit_ids=discovered,
        captured_commits=captured,
        record_errors=discovered - captured,
        duplicate_commit_ids=0,
        unavailable_objects=unavailable_count,
        parse_errors=parse_error_count,
        operational_errors=operational_errors,
        successful=successful,
    )
