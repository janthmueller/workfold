"""Atomic, bounded collection of Git commit file changes."""

from __future__ import annotations

import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from io import BytesIO
from typing import Final

from workfold.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer
from workfold.collection.git.batches import batched
from workfold.collection.git.changes.diff import (
    DiffCommitComplete,
    GitChangeParseError,
    ParsedGitChange,
    iter_diff_tree_name_status,
)
from workfold.collection.git.changes.models import (
    CollectedGitFileChange,
    GitFileChangeCollectionResult,
    GitFileChangeRepositoryAccounting,
)
from workfold.collection.git.changes.spool import GitChangeSpool, GitChangeSpoolError
from workfold.collection.git.commits.models import CollectedGitCommit
from workfold.collection.git.repository import GitRepository
from workfold.collection.git.runner import GitCommandError, GitRunner
from workfold.domain.observations import Source, TimestampKind
from workfold.domain.scope import ObservationScope

_EMPTY_TREE_BASIS: Final[str] = "empty-tree"
_DEFAULT_CHANGE_BATCH_SIZE: Final[int] = 2_048
_DEFAULT_CHANGE_BATCH_BYTES: Final[int] = 8 * 1_024 * 1_024
_DIFF_TREE_COMMAND: Final[tuple[str, ...]] = (
    "diff-tree",
    "--stdin",
    "--root",
    "-r",
    "--find-renames=50%",
    "--name-status",
    "-z",
    "--format=%H%x00",
    "--always",
)


class GitChangeReadError(RuntimeError):
    """Local I/O failure around a streamed diff-tree request."""


class GitFileChangeCollector:
    """Derive first-parent file changes in one Git process per repository."""

    def __init__(
        self,
        runner: GitRunner | None = None,
        *,
        commit_batch_size: int = 256,
        change_batch_size: int = _DEFAULT_CHANGE_BATCH_SIZE,
        change_batch_bytes: int = _DEFAULT_CHANGE_BATCH_BYTES,
    ) -> None:
        if commit_batch_size < 1:
            raise ValueError("commit_batch_size must be positive")
        if change_batch_size < 1:
            raise ValueError("change_batch_size must be positive")
        if change_batch_bytes < 1:
            raise ValueError("change_batch_bytes must be positive")
        self._runner = runner or GitRunner()
        self._commit_batch_size = commit_batch_size
        self._change_batch_size = change_batch_size
        self._change_batch_bytes = change_batch_bytes

    def collect(
        self,
        commits: Sequence[CollectedGitCommit],
        *,
        repositories: Sequence[GitRepository] = (),
        change_consumer: Callable[[tuple[CollectedGitFileChange, ...]], None] | None = None,
        timestamp_kinds: Sequence[TimestampKind] = (),
        observation_scope: ObservationScope | None = None,
        retain_changes: bool = True,
    ) -> GitFileChangeCollectionResult:
        """Collect changes for the exact supplied commit set without traversal."""

        requested_kinds = tuple(timestamp_kinds)
        kinds = tuple(dict.fromkeys(requested_kinds))
        if len(kinds) != len(requested_kinds) or any(
            kind not in {TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER} for kind in kinds
        ):
            raise ValueError("file-change scope accounting accepts unique Git author/committer timestamp kinds")

        grouped: dict[str, list[CollectedGitCommit]] = defaultdict(list)
        repositories_by_identity: dict[str, GitRepository] = {}
        for repository in repositories:
            repositories_by_identity.setdefault(repository.identity, repository)
        for commit in commits:
            identity = commit.repository.identity
            grouped[identity].append(commit)
            repositories_by_identity.setdefault(identity, commit.repository)

        changes: list[CollectedGitFileChange] = []
        diagnostics = DiagnosticBuffer()
        repository_accounting: list[GitFileChangeRepositoryAccounting] = []
        for identity, repository in repositories_by_identity.items():
            commit_group = grouped[identity]
            successful_for_repository = 0
            parse_errors_for_repository = 0
            subprocess_errors_for_repository = 0
            discovered_for_repository = 0
            scope_matches_for_repository: Counter[TimestampKind] = Counter()
            if not commit_group:
                repository_accounting.append(
                    GitFileChangeRepositoryAccounting(
                        repository=repository,
                        requested_commits=0,
                        successful_commits=0,
                        parse_errors=0,
                        subprocess_errors=0,
                        discovered_changes=0,
                        timestamp_kinds=kinds,
                        scope_matches=_freeze_timestamp_counts(kinds, scope_matches_for_repository),
                    )
                )
                continue
            for commit_batch in batched(commit_group, self._commit_batch_size):
                batch = tuple(commit_batch)
                expected_ids = tuple(item.commit.object_id for item in batch)
                by_id = {item.commit.object_id: item for item in batch}
                delivery_batch: list[CollectedGitFileChange] = []
                delivery_bytes = 0
                completed_commits = 0
                try:
                    with GitChangeSpool(memory_limit=self._change_batch_bytes) as pending_changes:
                        for record in _iter_diff_records(self._runner, repository, batch, expected_ids):
                            if not isinstance(record, DiffCommitComplete):
                                pending_changes.stage(record)
                                continue
                            for completed_change in pending_changes.release(record.commit_id):
                                commit_record = by_id[completed_change.commit_id]
                                item = CollectedGitFileChange(
                                    repository=repository,
                                    commit_record=commit_record,
                                    change=completed_change,
                                    diff_basis=(
                                        commit_record.commit.parent_ids[0]
                                        if commit_record.commit.parent_ids
                                        else _EMPTY_TREE_BASIS
                                    ),
                                )
                                retained_bytes = _retained_change_bytes(item)
                                if delivery_batch and delivery_bytes + retained_bytes > self._change_batch_bytes:
                                    _deliver_change_batch(
                                        delivery_batch,
                                        changes=changes,
                                        change_consumer=change_consumer,
                                        retain_changes=retain_changes,
                                    )
                                    delivery_bytes = 0
                                delivery_batch.append(item)
                                delivery_bytes += retained_bytes
                                discovered_for_repository += 1
                                for kind in kinds:
                                    signature = item.signature(kind)
                                    if observation_scope is None or observation_scope.includes_timestamp(
                                        instant_utc_ns=signature.epoch_nanoseconds,
                                        source=Source.GIT,
                                        actor_name=signature.identity.name,
                                        actor_email=signature.identity.email,
                                    ):
                                        scope_matches_for_repository[kind] += 1
                                if (
                                    len(delivery_batch) >= self._change_batch_size
                                    or delivery_bytes >= self._change_batch_bytes
                                ):
                                    _deliver_change_batch(
                                        delivery_batch,
                                        changes=changes,
                                        change_consumer=change_consumer,
                                        retain_changes=retain_changes,
                                    )
                                    delivery_bytes = 0
                            completed_commits += 1
                except GitCommandError as error:
                    diagnostics.append(_command_diagnostic(error, repository=repository))
                    failed_commits = _failed_commit_count(len(batch), completed_commits)
                    subprocess_errors_for_repository += failed_commits
                    successful_for_repository += len(batch) - failed_commits
                except GitChangeParseError as error:
                    failed_commits = _failed_commit_count(len(batch), completed_commits)
                    parse_errors_for_repository += failed_commits
                    successful_for_repository += len(batch) - failed_commits
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_file_change_parse",
                            target=os.fspath(repository.root),
                            provenance_id=error.commit_id,
                            message=str(error),
                            hint="The repository may be corrupt or may have changed during collection.",
                        )
                    )
                except GitChangeReadError as error:
                    failed_commits = _failed_commit_count(len(batch), completed_commits)
                    subprocess_errors_for_repository += failed_commits
                    successful_for_repository += len(batch) - failed_commits
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_file_change_io_error",
                            stage="git_file_change_discovery",
                            target=os.fspath(repository.root),
                            message=str(error),
                        )
                    )
                except GitChangeSpoolError as error:
                    failed_commits = _failed_commit_count(len(batch), completed_commits)
                    subprocess_errors_for_repository += failed_commits
                    successful_for_repository += len(batch) - failed_commits
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_file_change_spool_error",
                            stage="git_file_change_discovery",
                            target=os.fspath(repository.root),
                            message=str(error),
                        )
                    )
                else:
                    if completed_commits != len(batch):
                        raise AssertionError("diff-tree parser did not complete every requested commit")
                    successful_for_repository += completed_commits
                finally:
                    _deliver_change_batch(
                        delivery_batch,
                        changes=changes,
                        change_consumer=change_consumer,
                        retain_changes=retain_changes,
                    )
            repository_accounting.append(
                GitFileChangeRepositoryAccounting(
                    repository=repository,
                    requested_commits=len(commit_group),
                    successful_commits=successful_for_repository,
                    parse_errors=parse_errors_for_repository,
                    subprocess_errors=subprocess_errors_for_repository,
                    discovered_changes=discovered_for_repository,
                    timestamp_kinds=kinds,
                    scope_matches=_freeze_timestamp_counts(kinds, scope_matches_for_repository),
                )
            )

        return GitFileChangeCollectionResult(
            changes=tuple(changes),
            diagnostics=diagnostics.snapshot(),
            requested_commits=len(commits),
            successful_commits=sum(item.successful_commits for item in repository_accounting),
            discovered_changes=sum(item.discovered_changes for item in repository_accounting),
            parse_errors=sum(item.parse_errors for item in repository_accounting),
            subprocess_errors=sum(item.subprocess_errors for item in repository_accounting),
            repository_accounting=tuple(repository_accounting),
            records_retained=retain_changes,
        )


def _iter_diff_records(
    runner: GitRunner,
    repository: GitRepository,
    batch: tuple[CollectedGitCommit, ...],
    expected_ids: tuple[str, ...],
) -> Iterator[ParsedGitChange | DiffCommitComplete]:
    try:
        if not runner.streams_subprocess_output:
            input_data = b"".join(_diff_input_line(item) for item in batch)
            output = runner.run(_DIFF_TREE_COMMAND, cwd=repository.root, input_data=input_data).stdout
            yield from iter_diff_tree_name_status(BytesIO(output), expected_ids)
            return

        with tempfile.TemporaryFile() as requests:
            for item in batch:
                requests.write(_diff_input_line(item))
            requests.seek(0)
            final_completion: DiffCommitComplete | None = None
            with runner.open_stdout(
                _DIFF_TREE_COMMAND,
                cwd=repository.root,
                input_stream=requests,
            ) as stdout:
                for record in iter_diff_tree_name_status(stdout, expected_ids):
                    if isinstance(record, DiffCommitComplete) and record.at_stream_end:
                        final_completion = record
                    else:
                        yield record
            if final_completion is None:
                raise GitChangeParseError(
                    "missing_git_change_completion",
                    "diff-tree stream ended without completing its final commit",
                    commit_id=expected_ids[-1],
                )
            yield final_completion
    except OSError as error:
        raise GitChangeReadError(f"could not prepare or read the Git file-change stream: {error}") from error


def _command_diagnostic(error: GitCommandError, *, repository: GitRepository) -> CollectorDiagnostic:
    details = error.stderr_text
    message = str(error) if not details else f"{error}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage="git_file_change_discovery",
        target=os.fspath(repository.root),
        path=os.fspath(repository.root),
        message=message,
        hint=error.hint,
    )


def _diff_input_line(item: CollectedGitCommit) -> bytes:
    parent = item.commit.parent_ids[0] if item.commit.parent_ids else None
    line = item.commit.object_id if parent is None else f"{item.commit.object_id} {parent}"
    return line.encode("ascii") + b"\n"


def _failed_commit_count(requested: int, completed: int) -> int:
    if not 0 <= completed <= requested:
        raise AssertionError("completed diff-tree commit count is outside its request")
    return max(1, requested - completed)


def _deliver_change_batch(
    batch: list[CollectedGitFileChange],
    *,
    changes: list[CollectedGitFileChange],
    change_consumer: Callable[[tuple[CollectedGitFileChange, ...]], None] | None,
    retain_changes: bool,
) -> None:
    if not batch:
        return
    delivered = tuple(batch)
    batch.clear()
    if retain_changes:
        changes.extend(delivered)
    if change_consumer is not None:
        change_consumer(delivered)


def _retained_change_bytes(item: CollectedGitFileChange) -> int:
    change = item.change
    return (
        len(change.commit_id)
        + len(change.raw_status)
        + len(change.raw_path)
        + len(os.fsencode(change.path))
        + (0 if change.raw_old_path is None else len(change.raw_old_path))
        + (0 if change.old_path is None else len(os.fsencode(change.old_path)))
    )


def _freeze_timestamp_counts(
    kinds: tuple[TimestampKind, ...],
    counts: Counter[TimestampKind],
) -> tuple[tuple[TimestampKind, int], ...]:
    return tuple((kind, counts[kind]) for kind in kinds)


__all__ = ["GitChangeReadError", "GitFileChangeCollector"]
