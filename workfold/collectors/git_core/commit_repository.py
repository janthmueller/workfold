"""Collection and accounting for one semantic Git repository."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity
from workfold.collectors.git_core.commit_models import (
    CollectedGitCommit,
    CommitScopeDecision,
    CommitScopeFilter,
    GitCommitRepositoryAccounting,
    MaterializedCommitScopeFilter,
)
from workfold.collectors.git_core.object_model import (
    GitObjectParseError,
    GitSignatureRole,
    InvalidBatchCommit,
    RevListCommitScan,
    RevListScanSpec,
    UnavailableBatchObject,
    UnexpectedBatchObject,
)
from workfold.collectors.git_core.object_reader import (
    GitObjectReadError,
    iter_spooled_commit_objects,
    spool_commit_candidate,
)
from workfold.collectors.git_core.repository import GitRepository
from workfold.collectors.git_core.revision_scan import inspect_rev_list_scan
from workfold.collectors.git_core.revisions import iter_commit_scans_for_contexts
from workfold.collectors.git_core.runner import GitCommandError, GitRunner, command_diagnostic
from workfold.config import RefScope


def collect_repository_commits(
    repository: GitRepository,
    *,
    repository_contexts: Sequence[GitRepository],
    runner: GitRunner,
    ref_scope: RefScope,
    object_batch_size: int,
    object_batch_bytes: int,
    diagnostics: DiagnosticBuffer,
    commits: list[CollectedGitCommit],
    commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None,
    scan_spec: RevListScanSpec | None,
    commit_filter: CommitScopeFilter | None,
    materialized_filter: MaterializedCommitScopeFilter | None,
    retain_commits: bool,
) -> GitCommitRepositoryAccounting:
    """Discover, hydrate, and account commits for one object database."""

    error_count_start = diagnostics.error_count
    discovered = 0
    examined = 0
    candidate_count = 0
    selected_count = 0
    hydrated = 0
    unavailable_count = 0
    parse_error_count = 0
    candidate_parse_errors = 0
    timestamp_values_read: Counter[GitSignatureRole] = Counter()
    candidate_roles_read: Counter[GitSignatureRole] = Counter()
    evaluated_candidate_roles: Counter[GitSignatureRole] = Counter()
    scope_matches: Counter[GitSignatureRole] = Counter()
    materialized_scope_matches: Counter[GitSignatureRole] = Counter()
    discovery_completed = False
    object_read_failed = False
    effective_scan_spec = scan_spec or RevListScanSpec(("author", "committer"))
    effective_commit_filter = commit_filter or _select_all_scanned_roles
    timestamp_roles = effective_scan_spec.roles
    try:
        candidate_spool = tempfile.TemporaryFile()
    except OSError as error:
        diagnostics.append(_candidate_spool_diagnostic(repository, error))
    else:
        with candidate_spool:
            try:
                records = iter_commit_scans_for_contexts(
                    repository_contexts,
                    runner,
                    ref_scope,
                    effective_scan_spec,
                )
                for record in records:
                    discovered += 1
                    try:
                        scanned = inspect_rev_list_scan(record, effective_scan_spec)
                        decision = effective_commit_filter(repository, scanned)
                        candidate_roles = _normalize_scope_decision(decision, timestamp_roles)
                    except GitObjectParseError as error:
                        parse_error_count += 1
                        _append_commit_parse_diagnostic(diagnostics, repository, error)
                        continue
                    examined += 1
                    timestamp_values_read.update(timestamp_roles)
                    if candidate_roles:
                        candidate_count += 1
                        candidate_roles_read.update(candidate_roles)
                        spool_commit_candidate(candidate_spool, scanned.object_id, candidate_roles)
            except GitCommandError as error:
                diagnostics.append(command_diagnostic(error, stage="git_commit_discovery", target=repository.root))
            except GitObjectReadError as error:
                diagnostics.append(_object_io_diagnostic(repository, error))
                object_read_failed = True
            else:
                discovery_completed = True

            if materialized_filter is None:
                scope_matches.update(candidate_roles_read)

            if candidate_count and not object_read_failed:
                selected_batch: list[CollectedGitCommit] = []
                selected_batch_bytes = 0
                try:
                    candidate_objects = iter_spooled_commit_objects(
                        candidate_spool,
                        candidate_count=candidate_count,
                        runner=runner,
                        repository=repository,
                        fallback_batch_size=object_batch_size,
                    )
                    for candidate_roles, result in candidate_objects:
                        if isinstance(result, UnavailableBatchObject):
                            unavailable_count += 1
                            diagnostics.append(
                                CollectorDiagnostic(
                                    code="git_object_unavailable",
                                    stage="git_object_read",
                                    target=os.fspath(repository.root),
                                    provenance_id=result.requested_id,
                                    message=f"Git object is unavailable ({result.reason})",
                                    hint=(
                                        "The repository may be shallow or partial; "
                                        "Workfold will not fetch missing objects."
                                    ),
                                )
                            )
                            continue
                        if candidate_roles is None:
                            raise GitObjectParseError(
                                "invalid_commit_candidate",
                                "cat-file omitted commit candidate metadata",
                                object_id=result.object_id,
                            )
                        if isinstance(result, UnexpectedBatchObject):
                            candidate_parse_errors += 1
                            diagnostics.append(
                                CollectorDiagnostic(
                                    code="git_object_not_commit",
                                    stage="git_object_parse",
                                    target=os.fspath(repository.root),
                                    provenance_id=result.object_id,
                                    message=f"rev-list object has unexpected type {result.object_type!r}",
                                )
                            )
                            continue
                        if isinstance(result, InvalidBatchCommit):
                            candidate_parse_errors += 1
                            _append_commit_parse_diagnostic(
                                diagnostics,
                                repository,
                                GitObjectParseError(
                                    result.code,
                                    result.message,
                                    object_id=result.object_id,
                                ),
                            )
                            continue
                        parsed = result
                        if parsed.subject_truncated:
                            diagnostics.append(
                                CollectorDiagnostic(
                                    code="git_subject_truncated",
                                    stage="git_object_parse",
                                    target=os.fspath(repository.root),
                                    provenance_id=parsed.object_id,
                                    message=(
                                        "commit subject exceeds the retained metadata limit; "
                                        "timestamps and identities were preserved"
                                    ),
                                    severity=DiagnosticSeverity.WARNING,
                                )
                            )
                        hydrated += 1
                        evaluated_candidate_roles.update(candidate_roles)
                        exact_roles = (
                            candidate_roles
                            if materialized_filter is None
                            else _normalize_scope_decision(
                                materialized_filter(repository, parsed, candidate_roles),
                                candidate_roles,
                            )
                        )
                        if materialized_filter is not None:
                            scope_matches.update(exact_roles)
                        materialized_scope_matches.update(exact_roles)
                        if not exact_roles:
                            continue
                        selected_count += 1
                        collected = CollectedGitCommit(repository=repository, commit=parsed)
                        retained_bytes = _retained_commit_bytes(collected)
                        if selected_batch and selected_batch_bytes + retained_bytes > object_batch_bytes:
                            _deliver_commit_batch(
                                selected_batch,
                                commits=commits,
                                commit_consumer=commit_consumer,
                                retain_commits=retain_commits,
                            )
                            selected_batch_bytes = 0
                        selected_batch.append(collected)
                        selected_batch_bytes += retained_bytes
                        if len(selected_batch) >= object_batch_size or selected_batch_bytes >= object_batch_bytes:
                            _deliver_commit_batch(
                                selected_batch,
                                commits=commits,
                                commit_consumer=commit_consumer,
                                retain_commits=retain_commits,
                            )
                            selected_batch_bytes = 0
                except GitCommandError as error:
                    diagnostics.append(command_diagnostic(error, stage="git_object_read", target=repository.root))
                    object_read_failed = True
                except GitObjectParseError as error:
                    unresolved = candidate_count - hydrated - unavailable_count - candidate_parse_errors
                    candidate_parse_errors += max(0, unresolved)
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
                    object_read_failed = True
                except GitObjectReadError as error:
                    diagnostics.append(_object_io_diagnostic(repository, error))
                    object_read_failed = True
                finally:
                    _deliver_commit_batch(
                        selected_batch,
                        commits=commits,
                        commit_consumer=commit_consumer,
                        retain_commits=retain_commits,
                    )

    parse_error_count += candidate_parse_errors
    scope_evaluation_errors: Counter[GitSignatureRole] = Counter()
    if materialized_filter is not None:
        for role in timestamp_roles:
            scope_evaluation_errors[role] = candidate_roles_read[role] - evaluated_candidate_roles[role]
    successful = discovery_completed and not object_read_failed
    operational_errors = diagnostics.error_count - error_count_start

    return GitCommitRepositoryAccounting(
        repository=repository,
        discovered_commit_ids=discovered,
        examined_commits=examined,
        candidate_commits=candidate_count,
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
        scope_evaluation_errors=_freeze_role_counts(scope_evaluation_errors, timestamp_roles),
    )


def _select_all_scanned_roles(
    _repository: GitRepository,
    scanned: RevListCommitScan,
) -> CommitScopeDecision:
    return scanned.roles


def _deliver_commit_batch(
    batch: list[CollectedGitCommit],
    *,
    commits: list[CollectedGitCommit],
    commit_consumer: Callable[[tuple[CollectedGitCommit, ...]], None] | None,
    retain_commits: bool,
) -> None:
    if not batch:
        return
    delivered = tuple(batch)
    batch.clear()
    if retain_commits:
        commits.extend(delivered)
    if commit_consumer is not None:
        commit_consumer(delivered)


def _retained_commit_bytes(item: CollectedGitCommit) -> int:
    """Estimate variable-sized metadata retained until the next delivery."""

    commit = item.commit
    signatures = (commit.author, commit.committer)
    return (
        len(commit.object_id)
        + len(commit.tree_id)
        + sum(len(parent_id) for parent_id in commit.parent_ids)
        + len(commit.raw_subject)
        + len(commit.subject.encode("utf-8", errors="surrogateescape"))
        + (0 if commit.declared_encoding is None else len(commit.declared_encoding))
        + sum(
            len(signature.raw)
            + len(signature.identity.raw)
            + len(signature.identity.raw_name)
            + len(signature.identity.raw_email)
            + len(signature.raw_timestamp_bytes)
            for signature in signatures
        )
    )


def _candidate_spool_diagnostic(repository: GitRepository, error: OSError) -> CollectorDiagnostic:
    return CollectorDiagnostic(
        code="git_candidate_spool_error",
        stage="git_object_read",
        target=os.fspath(repository.root),
        message=f"temporary Git candidate inventory could not be read or written: {error}",
        hint="Check temporary-directory space and permissions.",
    )


def _object_io_diagnostic(repository: GitRepository, error: GitObjectReadError) -> CollectorDiagnostic:
    hint = (
        "Check temporary-directory space and permissions."
        if error.code == "git_candidate_spool_error"
        else "The repository or local Git process may have changed during collection."
    )
    return CollectorDiagnostic(
        code=error.code,
        stage="git_object_read",
        target=os.fspath(repository.root),
        message=str(error),
        hint=hint,
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


__all__ = ["collect_repository_commits"]
