"""Batched, NUL-safe collection of Git commit file changes."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic, CollectorResult
from workfold.collectors.git import (
    CollectedGitCommit,
    GitCommandError,
    GitRepository,
    GitRunner,
)
from workfold.iterables import batched
from workfold.models import (
    GitChangeKind,
    RecordKind,
    RecordOrigin,
    Source,
    TimestampKind,
    TimestampObservation,
)
from workfold.provenance import git_file_change_id

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_STATUS_RE: Final[re.Pattern[bytes]] = re.compile(rb"([A-Z])([0-9]{1,3})?\Z")
_EMPTY_TREE_BASIS: Final[str] = "empty-tree"


class GitChangeParseError(ValueError):
    """A structured failure to parse ``diff-tree -z`` output."""

    def __init__(self, code: str, message: str, *, commit_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.commit_id = commit_id


@dataclass(frozen=True, slots=True)
class ParsedGitChange:
    """One exact name-status record emitted for a selected commit."""

    commit_id: str
    raw_status: str
    change_kind: GitChangeKind
    path: Path
    raw_path: bytes
    old_path: Path | None = None
    raw_old_path: bytes | None = None
    similarity: int | None = None


@dataclass(frozen=True, slots=True)
class CollectedGitFileChange:
    """A parsed file change paired with its commit timestamp provenance."""

    repository: GitRepository
    commit_record: CollectedGitCommit
    change: ParsedGitChange
    diff_basis: str

    def to_origin(self) -> RecordOrigin:
        """Convert the change to the renderer-neutral domain model."""

        commit = self.commit_record.commit
        old_path = os.fsdecode(self.change.raw_old_path) if self.change.raw_old_path is not None else None
        path = os.fsdecode(self.change.raw_path)
        return RecordOrigin(
            record_id=git_file_change_id(
                self.repository.root,
                commit.object_id,
                self.diff_basis,
                self.change.raw_status,
                old_path,
                path,
            ),
            source=Source.GIT,
            record_kind=RecordKind.GIT_FILE_CHANGE,
            repository_or_root=self.repository.root,
            path=Path(path),
            old_path=Path(old_path) if old_path is not None else None,
            commit_id=commit.object_id,
            object_id=commit.object_id,
            diff_basis=self.diff_basis,
            change_kind=self.change.change_kind,
            author_name=commit.author.identity.name,
            author_email=commit.author.identity.email,
            description=commit.subject,
        )

    def to_observation(self, kind: TimestampKind) -> TimestampObservation:
        """Inherit one exact author/committer slot from the owning commit."""

        if kind is TimestampKind.GIT_AUTHOR:
            signature = self.commit_record.commit.author
        elif kind is TimestampKind.GIT_COMMITTER:
            signature = self.commit_record.commit.committer
        else:
            raise ValueError("file-change records support only Git author and committer timestamps")
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
class GitFileChangeRepositoryAccounting:
    """Reconciled file-change derivation counters for one repository."""

    repository: GitRepository
    requested_commits: int
    successful_commits: int
    parse_errors: int
    subprocess_errors: int
    discovered_changes: int

    def __post_init__(self) -> None:
        counters = (
            self.requested_commits,
            self.successful_commits,
            self.parse_errors,
            self.subprocess_errors,
            self.discovered_changes,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git file-change repository counters must be non-negative")
        terminal_commits = self.successful_commits + self.parse_errors + self.subprocess_errors
        if self.requested_commits != terminal_commits:
            raise ValueError("Git file-change repository commit accounting does not reconcile")

    @property
    def repository_root(self) -> Path:
        """Filesystem root used as the derivation coverage target."""

        return self.repository.root

    @property
    def repository_identity(self) -> str:
        """Canonical repository identity used for collection batching."""

        return self.repository.identity


@dataclass(frozen=True, slots=True)
class GitFileChangeCollectionResult:
    """File-change records and collection accounting."""

    changes: tuple[CollectedGitFileChange, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_commits: int
    successful_commits: int
    discovered_changes: int
    parse_errors: int
    subprocess_errors: int
    repository_accounting: tuple[GitFileChangeRepositoryAccounting, ...] = ()
    records_retained: bool = True

    def __post_init__(self) -> None:
        counters = (
            self.requested_commits,
            self.successful_commits,
            self.discovered_changes,
            self.parse_errors,
            self.subprocess_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git file-change collection counters must be non-negative")
        terminal_commits = self.successful_commits + self.parse_errors + self.subprocess_errors
        if self.requested_commits != terminal_commits:
            raise ValueError("Git file-change commit accounting does not reconcile")
        if self.records_retained and self.discovered_changes != len(self.changes):
            raise ValueError("Git file-change discovery count does not match captured records")
        if len(self.changes) > self.discovered_changes:
            raise ValueError("retained Git file changes exceed discovered records")
        if self.repository_accounting:
            aggregate = (
                sum(item.requested_commits for item in self.repository_accounting),
                sum(item.successful_commits for item in self.repository_accounting),
                sum(item.discovered_changes for item in self.repository_accounting),
                sum(item.parse_errors for item in self.repository_accounting),
                sum(item.subprocess_errors for item in self.repository_accounting),
            )
            expected = (
                self.requested_commits,
                self.successful_commits,
                self.discovered_changes,
                self.parse_errors,
                self.subprocess_errors,
            )
            if aggregate != expected:
                raise ValueError("Git file-change repository partitions do not match aggregate counters")

    @property
    def is_partial(self) -> bool:
        """Whether any requested repository batch could not be collected."""

        return bool(self.diagnostics)

    def to_domain_result(
        self,
        timestamp_kinds: Sequence[TimestampKind] = (TimestampKind.GIT_AUTHOR,),
    ) -> CollectorResult[RecordOrigin, TimestampObservation]:
        """Adapt raw changes to shared records and timestamp observations."""

        normalized_kinds = tuple(dict.fromkeys(timestamp_kinds))
        invalid = [
            kind for kind in normalized_kinds if kind not in {TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER}
        ]
        if invalid:
            names = ", ".join(kind.value for kind in invalid)
            raise ValueError(f"file-change records do not expose timestamp kind(s): {names}")
        return CollectorResult(
            origins=tuple(item.to_origin() for item in self.changes),
            observations=tuple(item.to_observation(kind) for item in self.changes for kind in normalized_kinds),
            diagnostics=self.diagnostics,
        )


def _change_kind(status: bytes) -> GitChangeKind:
    return {
        b"A": GitChangeKind.ADDED,
        b"M": GitChangeKind.MODIFIED,
        b"D": GitChangeKind.DELETED,
        b"R": GitChangeKind.RENAMED,
    }.get(status, GitChangeKind.OTHER)


def _decode_path(raw_path: bytes, *, commit_id: str) -> Path:
    if not raw_path:
        raise GitChangeParseError(
            "invalid_git_change_path",
            "git diff-tree returned an empty path",
            commit_id=commit_id,
        )
    return Path(os.fsdecode(raw_path))


def parse_diff_tree_name_status(
    payload: bytes,
    expected_commit_ids: tuple[str, ...],
) -> tuple[ParsedGitChange, ...]:
    """Parse a batched ``diff-tree --name-status -z`` response.

    The caller supplies commits to ``diff-tree --stdin`` in a known order and
    requests ``--format=%H%x00 --always``.  Each commit header therefore forms
    a validated boundary even for an empty diff.  Paths remain NUL-delimited,
    so tabs, newlines and undecodable filename bytes are retained.
    """

    if not expected_commit_ids:
        if payload:
            raise GitChangeParseError("unexpected_git_change_output", "diff-tree returned unrequested data")
        return ()

    tokens = payload.split(b"\0")
    cursor = 0
    parsed: list[ParsedGitChange] = []
    for index, expected_id in enumerate(expected_commit_ids):
        if cursor >= len(tokens) or tokens[cursor] != expected_id.encode("ascii"):
            raise GitChangeParseError(
                "unexpected_git_change_commit",
                "diff-tree response order does not match its request",
                commit_id=expected_id,
            )
        cursor += 1
        if cursor >= len(tokens) or tokens[cursor] != b"":
            raise GitChangeParseError(
                "invalid_git_change_boundary",
                "diff-tree commit header has no NUL boundary",
                commit_id=expected_id,
            )
        cursor += 1

        next_id = expected_commit_ids[index + 1].encode("ascii") if index + 1 < len(expected_commit_ids) else None
        first_status = True
        while cursor < len(tokens):
            token = tokens[cursor]
            if next_id is not None and token == next_id:
                break
            if next_id is None and token == b"" and cursor == len(tokens) - 1:
                cursor += 1
                break
            if first_status and token.startswith(b"\n"):
                token = token[1:]
            first_status = False
            match = _STATUS_RE.fullmatch(token)
            if match is None:
                raise GitChangeParseError(
                    "invalid_git_change_status",
                    "diff-tree returned an invalid name-status token",
                    commit_id=expected_id,
                )
            status_letter, score_raw = match.groups()
            similarity = int(score_raw) if score_raw is not None else None
            if similarity is not None and similarity > 100:
                raise GitChangeParseError(
                    "invalid_git_change_status",
                    "diff-tree returned an out-of-range similarity score",
                    commit_id=expected_id,
                )
            cursor += 1
            path_count = 2 if status_letter in {b"R", b"C"} else 1
            if cursor + path_count > len(tokens):
                raise GitChangeParseError(
                    "truncated_git_change",
                    "diff-tree output ended inside a path record",
                    commit_id=expected_id,
                )
            if path_count == 2:
                raw_old_path = tokens[cursor]
                raw_path = tokens[cursor + 1]
                cursor += 2
            else:
                raw_old_path = None
                raw_path = tokens[cursor]
                cursor += 1
            parsed.append(
                ParsedGitChange(
                    commit_id=expected_id,
                    raw_status=token.decode("ascii"),
                    change_kind=_change_kind(status_letter),
                    path=_decode_path(raw_path, commit_id=expected_id),
                    raw_path=raw_path,
                    old_path=(_decode_path(raw_old_path, commit_id=expected_id) if raw_old_path is not None else None),
                    raw_old_path=raw_old_path,
                    similarity=similarity,
                )
            )

    if cursor != len(tokens):
        raise GitChangeParseError(
            "unexpected_git_change_output",
            "diff-tree returned trailing unrequested data",
        )
    return tuple(parsed)


def _diagnostic(
    error: GitCommandError,
    *,
    repository: GitRepository,
) -> CollectorDiagnostic:
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


class GitFileChangeCollector:
    """Derive first-parent file changes in one Git process per repository."""

    def __init__(self, runner: GitRunner | None = None, *, commit_batch_size: int = 256) -> None:
        if commit_batch_size < 1:
            raise ValueError("commit_batch_size must be positive")
        self._runner = runner or GitRunner()
        self._commit_batch_size = commit_batch_size

    def collect(
        self,
        commits: Sequence[CollectedGitCommit],
        *,
        repositories: Sequence[GitRepository] = (),
        change_consumer: Callable[[tuple[CollectedGitFileChange, ...]], None] | None = None,
        retain_changes: bool = True,
    ) -> GitFileChangeCollectionResult:
        """Collect changes for the exact supplied commit set without traversal."""

        grouped: dict[str, list[CollectedGitCommit]] = defaultdict(list)
        repositories_by_identity: dict[str, GitRepository] = {}
        for repository in repositories:
            repositories_by_identity.setdefault(repository.identity, repository)
        for commit in commits:
            identity = commit.repository.identity
            grouped[identity].append(commit)
            repositories_by_identity.setdefault(identity, commit.repository)

        changes: list[CollectedGitFileChange] = []
        diagnostics: list[CollectorDiagnostic] = []
        repository_accounting: list[GitFileChangeRepositoryAccounting] = []
        for identity, repository in repositories_by_identity.items():
            commit_group = grouped[identity]
            successful_for_repository = 0
            parse_errors_for_repository = 0
            subprocess_errors_for_repository = 0
            discovered_for_repository = 0
            if not commit_group:
                repository_accounting.append(
                    GitFileChangeRepositoryAccounting(
                        repository=repository,
                        requested_commits=0,
                        successful_commits=0,
                        parse_errors=0,
                        subprocess_errors=0,
                        discovered_changes=0,
                    )
                )
                continue
            for commit_batch in batched(commit_group, self._commit_batch_size):
                batch = tuple(commit_batch)
                expected_ids = tuple(item.commit.object_id for item in batch)
                input_lines: list[bytes] = []
                for item in batch:
                    parent = item.commit.parent_ids[0] if item.commit.parent_ids else None
                    line = item.commit.object_id if parent is None else f"{item.commit.object_id} {parent}"
                    input_lines.append(line.encode("ascii") + b"\n")
                try:
                    output = self._runner.run(
                        (
                            "diff-tree",
                            "--stdin",
                            "--root",
                            "-r",
                            "--find-renames=50%",
                            "--name-status",
                            "-z",
                            "--format=%H%x00",
                            "--always",
                        ),
                        cwd=repository.root,
                        input_data=b"".join(input_lines),
                    ).stdout
                    parsed = parse_diff_tree_name_status(output, expected_ids)
                except GitCommandError as error:
                    diagnostics.append(_diagnostic(error, repository=repository))
                    subprocess_errors_for_repository += len(batch)
                except GitChangeParseError as error:
                    parse_errors_for_repository += len(batch)
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
                else:
                    by_id = {item.commit.object_id: item for item in batch}
                    collected_batch: list[CollectedGitFileChange] = []
                    for change in parsed:
                        commit_record = by_id[change.commit_id]
                        diff_basis = (
                            commit_record.commit.parent_ids[0] if commit_record.commit.parent_ids else _EMPTY_TREE_BASIS
                        )
                        collected_batch.append(
                            CollectedGitFileChange(
                                repository=repository,
                                commit_record=commit_record,
                                change=change,
                                diff_basis=diff_basis,
                            )
                        )
                    discovered_for_repository += len(collected_batch)
                    if retain_changes:
                        changes.extend(collected_batch)
                    if collected_batch and change_consumer is not None:
                        change_consumer(tuple(collected_batch))
                    successful_for_repository += len(batch)
            repository_accounting.append(
                GitFileChangeRepositoryAccounting(
                    repository=repository,
                    requested_commits=len(commit_group),
                    successful_commits=successful_for_repository,
                    parse_errors=parse_errors_for_repository,
                    subprocess_errors=subprocess_errors_for_repository,
                    discovered_changes=discovered_for_repository,
                )
            )

        successful_commits = sum(item.successful_commits for item in repository_accounting)
        parse_errors = sum(item.parse_errors for item in repository_accounting)
        subprocess_errors = sum(item.subprocess_errors for item in repository_accounting)

        discovered_changes = sum(item.discovered_changes for item in repository_accounting)
        return GitFileChangeCollectionResult(
            changes=tuple(changes),
            diagnostics=tuple(diagnostics),
            requested_commits=len(commits),
            successful_commits=successful_commits,
            discovered_changes=discovered_changes,
            parse_errors=parse_errors,
            subprocess_errors=subprocess_errors,
            repository_accounting=tuple(repository_accounting),
            records_retained=retain_changes,
        )


__all__ = [
    "CollectedGitFileChange",
    "GitChangeParseError",
    "GitFileChangeCollectionResult",
    "GitFileChangeCollector",
    "GitFileChangeRepositoryAccounting",
    "ParsedGitChange",
    "parse_diff_tree_name_status",
]
