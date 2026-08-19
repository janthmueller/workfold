"""Semantic local reflog collection, including non-commit object updates.

Git's formatted ``reflog show`` is built on revision walking and silently omits
entries whose new object is not a commit.  Wuf therefore asks Git which
reflogs exist and resolves each semantic log path with ``rev-parse --git-path``
before parsing the documented reflog record representation.  These files are
never treated as filesystem activity metadata.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import Final

from wuf.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer
from wuf.collection.git.command_error import GitCommandError
from wuf.collection.git.reflogs.models import (
    CollectedGitReflog,
    GitReflogCollectionResult,
    GitReflogParseError,
    GitReflogReadError,
    ParsedReflogEntry,
    ReflogRef,
    ReflogVisit,
)
from wuf.collection.git.reflogs.parser import (
    parse_current_refs,
    parse_reflog_entries,
    parse_reflog_list,
    parse_reflog_selectors,
)
from wuf.collection.git.reflogs.reader import decode_git_path, read_semantic_reflog
from wuf.collection.git.reflogs.spill import visit_semantic_reflog
from wuf.collection.git.repository import GitRepository
from wuf.collection.git.runner import GitRunner
from wuf.domain.observations import Source
from wuf.domain.scope import ObservationScope

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_EPOCH_RE: Final[re.Pattern[bytes]] = re.compile(rb"-?[0-9]+\Z")
_OFFSET_RE: Final[re.Pattern[bytes]] = re.compile(rb"[+-][0-9]{4}\Z")
_MIN_LOCALIZABLE_EPOCH: Final[int] = -62_135_510_400
_MAX_LOCALIZABLE_EPOCH: Final[int] = 253_402_214_399
_PER_WORKTREE_REF_PREFIXES: Final[tuple[str, ...]] = (
    "refs/bisect/",
    "refs/rewritten/",
    "refs/worktree/",
)


SemanticReflogReader = Callable[..., tuple[bytes, bool]]
SemanticReflogVisitor = Callable[..., ReflogVisit]


def _command_diagnostic(
    error: GitCommandError,
    *,
    repository: GitRepository,
    stage: str,
) -> CollectorDiagnostic:
    details = error.stderr_text
    message = str(error) if not details else f"{error}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage=stage,
        target=os.fspath(repository.root),
        path=os.fspath(repository.root),
        message=message,
        hint=error.hint,
        category=error.category,
    )


def _reflog_inventory_key(repository: GitRepository, ref_name: str) -> tuple[str, str]:
    """Return a key that separates worktree-local logs from shared logs."""

    is_worktree_local = not ref_name.startswith("refs/") or ref_name.startswith(_PER_WORKTREE_REF_PREFIXES)
    scope_identity = repository.context_identity if is_worktree_local else repository.identity
    return scope_identity, ref_name


def discover_reflog_names(runner: GitRunner, repository: GitRepository) -> tuple[str, ...]:
    """Use efficient reflog inventory when available, with an older-Git fallback."""

    try:
        payload = runner.run(("reflog", "list"), cwd=repository.root).stdout
    except GitCommandError:
        payload = runner.run(
            ("reflog", "show", "--all", "--format=%gD"),
            cwd=repository.root,
        ).stdout
        return parse_reflog_selectors(payload)
    return parse_reflog_list(payload)


class GitReflogCollector:
    """Enumerate and read local semantic reflogs without revision-walk loss."""

    def __init__(
        self,
        runner: GitRunner | None = None,
        *,
        reflog_reader: SemanticReflogReader = read_semantic_reflog,
        reflog_visitor: SemanticReflogVisitor | None = visit_semantic_reflog,
    ) -> None:
        self._runner = runner or GitRunner()
        self._reflog_reader = reflog_reader
        self._reflog_visitor = reflog_visitor

    def collect(
        self,
        repositories: Sequence[GitRepository],
        *,
        entry_consumer: Callable[[tuple[CollectedGitReflog, ...]], None] | None = None,
        observation_scope: ObservationScope | None = None,
        retain_entries: bool = True,
    ) -> GitReflogCollectionResult:
        """Collect reflogs independently of commit ref scope and identity filters."""

        collected: list[CollectedGitReflog] = []
        available_statuses: list[ReflogRef] = []
        unavailable_statuses: list[ReflogRef] = []
        diagnostics = DiagnosticBuffer()
        successful = 0
        discovered_ref_count = 0
        unavailable_entry_count = 0
        captured_entry_count = 0
        parse_errors = 0
        seen_inventory: set[tuple[str, str]] = set()

        for repository in repositories:
            repository_failed = False
            try:
                current_output = self._runner.run(
                    ("show-ref", "--head"),
                    cwd=repository.root,
                    allowed_returncodes=(0, 1),
                ).stdout
                current_refs = parse_current_refs(current_output)
                available_names = discover_reflog_names(self._runner, repository)
            except GitCommandError as error:
                diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_reflog_discovery"))
                continue
            except GitReflogParseError as error:
                parse_errors += 1
                diagnostics.append(
                    CollectorDiagnostic(
                        code=error.code,
                        stage="git_reflog_discovery",
                        target=os.fspath(repository.root),
                        provenance_id=error.ref_name,
                        message=str(error),
                    )
                )
                continue

            available_set = set(available_names)
            for ref_name in current_refs:
                if ref_name in available_set:
                    continue
                inventory_key = _reflog_inventory_key(repository, ref_name)
                if inventory_key in seen_inventory:
                    continue
                seen_inventory.add(inventory_key)
                discovered_ref_count += 1
                unavailable_statuses.append(ReflogRef(repository, ref_name, 0, 0))
            for ref_name in available_names:
                inventory_key = _reflog_inventory_key(repository, ref_name)
                if inventory_key in seen_inventory:
                    continue
                seen_inventory.add(inventory_key)
                discovered_ref_count += 1
                raw_record_count = 0
                captured_for_ref = 0
                scope_matches_for_ref = 0
                try:
                    path_output = self._runner.run(
                        (
                            "rev-parse",
                            "--git-path",
                            f"logs/{ref_name}",
                        ),
                        cwd=repository.root,
                    ).stdout
                    reflog_path = decode_git_path(path_output, repository=repository)

                    def consume_parsed(
                        parsed_batch: tuple[ParsedReflogEntry, ...],
                        current_repository: GitRepository = repository,
                    ) -> None:
                        nonlocal captured_for_ref, captured_entry_count, scope_matches_for_ref
                        collected_batch = tuple(
                            CollectedGitReflog(repository=current_repository, entry=entry) for entry in parsed_batch
                        )
                        captured_for_ref += len(collected_batch)
                        captured_entry_count += len(collected_batch)
                        scope_matches_for_ref += sum(
                            _reflog_entry_matches_scope(entry, observation_scope) for entry in parsed_batch
                        )
                        if retain_entries:
                            collected.extend(collected_batch)
                        if collected_batch and entry_consumer is not None:
                            entry_consumer(collected_batch)

                    if self._reflog_visitor is not None:
                        visit = self._reflog_visitor(
                            reflog_path,
                            repository=repository,
                            ref_name=ref_name,
                            entry_consumer=consume_parsed,
                        )
                        raw_record_count = visit.entry_count
                        changed = visit.changed_during_read
                        if captured_for_ref != visit.captured_entry_count:
                            raise RuntimeError("reflog visitor capture accounting did not reconcile")
                    else:
                        # Preserve the public reader as a test/integration seam.
                        payload, changed = self._reflog_reader(reflog_path, repository=repository)
                        raw_record_count = payload.count(b"\n")
                        parsed_entries = parse_reflog_entries(payload, ref_name=ref_name)
                        for start in range(0, len(parsed_entries), 512):
                            consume_parsed(parsed_entries[start : start + 512])
                except GitCommandError as error:
                    repository_failed = True
                    diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_reflog_path"))
                    available_statuses.append(ReflogRef(repository, ref_name, 0, 0))
                    continue
                except GitReflogReadError as error:
                    repository_failed = True
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_reflog_read",
                            target=os.fspath(repository.root),
                            path=os.fspath(error.path) if error.path is not None else None,
                            provenance_id=ref_name,
                            message=str(error),
                        )
                    )
                    available_statuses.append(ReflogRef(repository, ref_name, 0, 0))
                    continue
                except GitReflogParseError as error:
                    repository_failed = True
                    parse_errors += 1
                    raw_record_count = max(raw_record_count, error.record_count)
                    unavailable_entry_count += raw_record_count
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_reflog_parse",
                            target=os.fspath(repository.root),
                            provenance_id=error.ref_name,
                            message=str(error),
                        )
                    )
                    available_statuses.append(ReflogRef(repository, ref_name, raw_record_count, 0))
                    continue
                if changed:
                    repository_failed = True
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_reflog_changed_during_collection",
                            stage="git_reflog_read",
                            target=os.fspath(repository.root),
                            path=os.fspath(reflog_path),
                            provenance_id=ref_name,
                            message="The reflog changed while Wuf read it; the captured snapshot may be partial",
                        )
                    )
                available_statuses.append(
                    ReflogRef(
                        repository,
                        ref_name,
                        raw_record_count,
                        captured_for_ref,
                        scope_matches_for_ref,
                    )
                )
            if not repository_failed:
                successful += 1

        return GitReflogCollectionResult(
            entries=tuple(collected),
            available_refs=tuple(available_statuses),
            refs_without_reflog=tuple(unavailable_statuses),
            diagnostics=diagnostics.snapshot(),
            requested_repositories=len(repositories),
            successful_repositories=successful,
            discovered_refs=discovered_ref_count,
            captured_entries=captured_entry_count,
            unavailable_entries=unavailable_entry_count,
            parse_errors=parse_errors,
            records_retained=retain_entries,
        )


def _reflog_entry_matches_scope(entry: ParsedReflogEntry, scope: ObservationScope | None) -> bool:
    if scope is None:
        return True
    return scope.includes_timestamp(
        instant_utc_ns=entry.epoch_nanoseconds,
        source=Source.GIT,
        actor_name=entry.actor_name,
        actor_email=entry.actor_email,
    )


__all__ = [
    "CollectedGitReflog",
    "GitReflogCollectionResult",
    "GitReflogCollector",
    "GitReflogParseError",
    "GitReflogReadError",
    "ParsedReflogEntry",
    "ReflogVisit",
    "ReflogRef",
    "parse_current_refs",
    "parse_reflog_entries",
    "parse_reflog_list",
    "parse_reflog_selectors",
    "discover_reflog_names",
    "read_semantic_reflog",
    "visit_semantic_reflog",
]
