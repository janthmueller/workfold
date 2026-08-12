"""Reachable Git commit enumeration, parsing, and accounting."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.git_core.repository import (
    GitRepository,
    GitRepositoryResolver,
    group_semantic_repositories,
)
from workfold.collectors.git_core.revisions import (
    iter_commit_metadata_for_contexts,
    iter_commit_scans_for_contexts,
)
from workfold.collectors.git_core.runner import GitCommandError, GitRunner, command_diagnostic
from workfold.collectors.git_objects import (
    GitObjectParseError,
    GitSignatureRole,
    ParsedCommit,
    RevListCommitScan,
    RevListScanSpec,
    inspect_rev_list_scan,
    parse_cat_file_batch,
    parse_commit_object,
    parse_rev_list_metadata,
)
from workfold.config import RefScope
from workfold.iterables import batched
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_commit_id

CommitScopeDecision = tuple[GitSignatureRole, ...]
CommitScopeFilter = Callable[[GitRepository, RevListCommitScan], CommitScopeDecision | bool]


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
    examined_commits: int
    selected_commits: int
    hydrated_commits: int
    duplicate_commit_ids: int
    unavailable_objects: int
    parse_errors: int
    operational_errors: int
    successful: bool
    timestamp_roles: tuple[GitSignatureRole, ...]
    timestamp_values_read: tuple[tuple[GitSignatureRole, int], ...] = ()
    scope_matches: tuple[tuple[GitSignatureRole, int], ...] = ()
    materialized_scope_matches: tuple[tuple[GitSignatureRole, int], ...] = ()

    def __post_init__(self) -> None:
        counters = (
            self.discovered_commit_ids,
            self.examined_commits,
            self.selected_commits,
            self.hydrated_commits,
            self.duplicate_commit_ids,
            self.unavailable_objects,
            self.parse_errors,
            self.operational_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git commit repository counters must be non-negative")
        if self.examined_commits > self.discovered_commit_ids:
            raise ValueError("examined Git commits exceed reachable commits")
        if self.selected_commits > self.examined_commits:
            raise ValueError("selected Git commits exceed examined commits")
        if self.hydrated_commits > self.selected_commits:
            raise ValueError("hydrated Git commits exceed selected commits")
        _validate_roles(self.timestamp_roles)
        for label, counts in (
            ("timestamp values read", self.timestamp_values_read),
            ("scope matches", self.scope_matches),
            ("materialized scope matches", self.materialized_scope_matches),
        ):
            _validate_role_counts(counts, roles=self.timestamp_roles, label=label)
        for role in self.timestamp_roles:
            values_read = self.timestamp_value_count(role)
            matches = self.scope_match_count(role)
            materialized = self.materialized_scope_match_count(role)
            if values_read != self.examined_commits:
                raise ValueError(f"Git {role} timestamp values must equal examined commits")
            if matches > values_read:
                raise ValueError(f"Git {role} scope matches exceed timestamp values read")
            if matches > self.selected_commits:
                raise ValueError(f"Git {role} scope matches exceed selected commits")
            if materialized > matches:
                raise ValueError(f"materialized Git {role} scope matches exceed matches")
            if materialized > self.hydrated_commits:
                raise ValueError(f"materialized Git {role} scope matches exceed hydrated commits")
        if self.selected_commits > sum(count for _, count in self.scope_matches):
            raise ValueError("selected Git commits exceed matching timestamp roles")
        if self.hydrated_commits > sum(count for _, count in self.materialized_scope_matches):
            raise ValueError("hydrated Git commits exceed materialized timestamp roles")

    @property
    def repository_root(self) -> Path:
        return self.repository.root

    @property
    def repository_identity(self) -> str:
        return self.repository.identity

    @property
    def eligible_commits(self) -> int:
        """Return records whose requested evidence was usable for coverage."""

        return self.discovered_commit_ids - self.record_errors

    @property
    def scope_errors(self) -> int:
        """Return records whose timestamp scope could not be evaluated."""

        return self.discovered_commit_ids - self.examined_commits

    @property
    def hydration_errors(self) -> int:
        """Return selected records whose rich provenance could not be read."""

        return self.selected_commits - self.hydrated_commits

    @property
    def record_errors(self) -> int:
        """Return all records that could hide a requested observation."""

        return self.scope_errors + self.hydration_errors

    def timestamp_value_count(self, role: GitSignatureRole) -> int:
        """Return successfully read lightweight timestamp values for one role."""

        return _role_count(self.timestamp_values_read, role)

    def scope_match_count(self, role: GitSignatureRole) -> int:
        """Return timestamp values independently matched to query scope."""

        return _role_count(self.scope_matches, role)

    def materialized_scope_match_count(self, role: GitSignatureRole) -> int:
        """Return matching roles whose full commit object was materialized."""

        return _role_count(self.materialized_scope_matches, role)

    def materialization_error_count(self, role: GitSignatureRole) -> int:
        """Return matching roles lost during rich commit hydration."""

        return self.scope_match_count(role) - self.materialized_scope_match_count(role)


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
        hydrated = sum(item.hydrated_commits for item in self.repository_accounting)
        if self.repository_accounting:
            if self.records_retained and len(self.commits) != hydrated:
                raise ValueError("retained Git commits do not match repository accounting")
            if len(self.commits) > hydrated:
                raise ValueError("retained Git commits exceed hydrated repository accounting")

    @property
    def is_partial(self) -> bool:
        return bool(self.diagnostics)


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
        scan_spec: RevListScanSpec | None = None,
        commit_filter: CommitScopeFilter | None = None,
        retain_commits: bool = True,
    ) -> GitCollectionResult:
        if (scan_spec is None) != (commit_filter is None):
            raise ValueError("commit scan spec and filter must be supplied together")
        if commit_filter is not None and retain_commits:
            raise ValueError("filtered commit collection cannot retain a complete record inventory")
        resolution = GitRepositoryResolver(self._runner).resolve(paths)
        diagnostics = DiagnosticBuffer()
        diagnostics.extend(resolution.diagnostics)
        repositories = list(resolution.repositories)
        commits: list[CollectedGitCommit] = []
        repository_accounting: list[GitCommitRepositoryAccounting] = []

        for repository_contexts in group_semantic_repositories(repositories):
            repository = repository_contexts[0]
            accounting = _collect_repository_commits(
                repository,
                repository_contexts=repository_contexts,
                runner=self._runner,
                ref_scope=ref_scope,
                object_batch_size=self._object_batch_size,
                diagnostics=diagnostics,
                commits=commits,
                commit_consumer=commit_consumer,
                scan_spec=scan_spec,
                commit_filter=commit_filter,
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
    repository_contexts: Sequence[GitRepository],
    runner: GitRunner,
    ref_scope: RefScope,
    object_batch_size: int,
    diagnostics: DiagnosticBuffer,
    commits: list[CollectedGitCommit],
    commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None,
    scan_spec: RevListScanSpec | None,
    commit_filter: CommitScopeFilter | None,
    retain_commits: bool,
) -> GitCommitRepositoryAccounting:
    error_count_start = diagnostics.error_count
    discovered = 0
    examined = 0
    selected_count = 0
    hydrated = 0
    unavailable_count = 0
    parse_error_count = 0
    timestamp_values_read: Counter[GitSignatureRole] = Counter()
    scope_matches: Counter[GitSignatureRole] = Counter()
    materialized_scope_matches: Counter[GitSignatureRole] = Counter()
    successful = False
    object_read_failed = False
    timestamp_roles: tuple[GitSignatureRole, ...] = ("author", "committer") if scan_spec is None else scan_spec.roles
    try:
        try:
            records = (
                iter_commit_metadata_for_contexts(repository_contexts, runner, ref_scope)
                if scan_spec is None
                else iter_commit_scans_for_contexts(repository_contexts, runner, ref_scope, scan_spec)
            )
            for record_batch in batched(
                records,
                object_batch_size,
            ):
                discovered += len(record_batch)
                hydrated_batch: list[CollectedGitCommit] = []
                if scan_spec is None:
                    for record in record_batch:
                        try:
                            parsed = parse_rev_list_metadata(record)
                        except GitObjectParseError as error:
                            parse_error_count += 1
                            _append_commit_parse_diagnostic(diagnostics, repository, error)
                            continue
                        examined += 1
                        selected_count += 1
                        hydrated += 1
                        for role in ("author", "committer"):
                            timestamp_values_read[role] += 1
                            scope_matches[role] += 1
                            materialized_scope_matches[role] += 1
                        hydrated_batch.append(CollectedGitCommit(repository=repository, commit=parsed))
                else:
                    assert commit_filter is not None
                    selected_ids: list[str] = []
                    selected_roles_by_id: dict[str, CommitScopeDecision] = {}
                    for record in record_batch:
                        try:
                            scanned = inspect_rev_list_scan(record, scan_spec)
                            decision = commit_filter(repository, scanned)
                            selected_roles = _normalize_scope_decision(decision, scan_spec.roles)
                        except GitObjectParseError as error:
                            parse_error_count += 1
                            _append_commit_parse_diagnostic(diagnostics, repository, error)
                            continue
                        examined += 1
                        timestamp_values_read.update(scan_spec.roles)
                        scope_matches.update(selected_roles)
                        if selected_roles:
                            selected_count += 1
                            selected_ids.append(scanned.object_id)
                            selected_roles_by_id[scanned.object_id] = selected_roles
                    if selected_ids:
                        try:
                            batch_output = runner.run(
                                ("cat-file", "--batch"),
                                cwd=repository.root,
                                input_data=b"".join(object_id.encode("ascii") + b"\n" for object_id in selected_ids),
                            ).stdout
                            object_batch = parse_cat_file_batch(batch_output, tuple(selected_ids))
                        except GitCommandError as error:
                            diagnostics.append(
                                command_diagnostic(error, stage="git_object_read", target=repository.root)
                            )
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
                            parse_error_count += len(selected_ids)
                            object_read_failed = True
                            continue

                        for unavailable in object_batch.unavailable:
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
                        for batch_object in object_batch.objects:
                            if batch_object.object_type != "commit":
                                parse_error_count += 1
                                diagnostics.append(
                                    CollectorDiagnostic(
                                        code="git_object_not_commit",
                                        stage="git_object_parse",
                                        target=os.fspath(repository.root),
                                        provenance_id=batch_object.object_id,
                                        message=(f"rev-list object has unexpected type {batch_object.object_type!r}"),
                                    )
                                )
                                continue
                            try:
                                parsed = parse_commit_object(batch_object.object_id, batch_object.data)
                            except GitObjectParseError as error:
                                parse_error_count += 1
                                _append_commit_parse_diagnostic(diagnostics, repository, error)
                                continue
                            hydrated += 1
                            materialized_scope_matches.update(selected_roles_by_id[batch_object.object_id])
                            hydrated_batch.append(CollectedGitCommit(repository=repository, commit=parsed))

                if retain_commits:
                    commits.extend(hydrated_batch)
                if hydrated_batch and commit_consumer is not None:
                    commit_consumer(tuple(hydrated_batch))
        except GitCommandError as error:
            diagnostics.append(command_diagnostic(error, stage="git_commit_discovery", target=repository.root))
        else:
            successful = not object_read_failed
    finally:
        operational_errors = diagnostics.error_count - error_count_start

    return GitCommitRepositoryAccounting(
        repository=repository,
        discovered_commit_ids=discovered,
        examined_commits=examined,
        selected_commits=selected_count,
        hydrated_commits=hydrated,
        duplicate_commit_ids=0,
        unavailable_objects=unavailable_count,
        parse_errors=parse_error_count,
        operational_errors=operational_errors,
        successful=successful,
        timestamp_roles=timestamp_roles,
        timestamp_values_read=_freeze_role_counts(timestamp_values_read, timestamp_roles),
        scope_matches=_freeze_role_counts(scope_matches, timestamp_roles),
        materialized_scope_matches=_freeze_role_counts(materialized_scope_matches, timestamp_roles),
    )


def _normalize_scope_decision(
    decision: CommitScopeDecision | bool,
    requested_roles: tuple[GitSignatureRole, ...],
) -> CommitScopeDecision:
    roles = requested_roles if decision is True else () if decision is False else tuple(decision)
    if len(set(roles)) != len(roles) or any(role not in requested_roles for role in roles):
        raise ValueError("commit scope decision must contain unique requested timestamp roles")
    return roles


def _freeze_role_counts(
    counts: Counter[GitSignatureRole],
    roles: tuple[GitSignatureRole, ...],
) -> tuple[tuple[GitSignatureRole, int], ...]:
    return tuple((role, counts[role]) for role in roles)


def _validate_roles(roles: tuple[GitSignatureRole, ...]) -> None:
    if not roles or len(set(roles)) != len(roles) or any(role not in {"author", "committer"} for role in roles):
        raise ValueError("Git timestamp roles must contain unique author/committer roles")


def _validate_role_counts(
    counts: tuple[tuple[GitSignatureRole, int], ...],
    *,
    roles: tuple[GitSignatureRole, ...],
    label: str,
) -> None:
    count_roles = tuple(role for role, _ in counts)
    if count_roles != roles:
        raise ValueError(f"Git {label} must cover the requested timestamp roles exactly once")
    if any(count < 0 for _, count in counts):
        raise ValueError(f"Git {label} must be non-negative")


def _role_count(counts: tuple[tuple[GitSignatureRole, int], ...], role: GitSignatureRole) -> int:
    return next((count for candidate, count in counts if candidate == role), 0)


def _append_commit_parse_diagnostic(
    diagnostics: DiagnosticBuffer,
    repository: GitRepository,
    error: GitObjectParseError,
) -> None:
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
