"""Translate Git collector accounting into source-owned coverage fragments."""

from __future__ import annotations

import os
from collections.abc import Mapping

from workfold.collection.git.changes.models import GitFileChangeCollectionResult
from workfold.collection.git.commits.models import GitCollectionResult
from workfold.collection.git.objects.models import GitSignatureRole
from workfold.collection.git.reflogs.models import GitReflogCollectionResult
from workfold.collection.git.tags.models import GitTagCollectionResult
from workfold.domain.coverage import (
    CollectionTimestampCoverage,
    CoverageFragment,
    RecordCoverage,
    RecordCoverageKey,
    TimestampCoverageKey,
)
from workfold.domain.observations import RecordKind, Source, TimestampKind

GIT_COVERAGE_TARGET = "selected Git repositories"


def build_git_coverage_fragment(
    *,
    commit_result: GitCollectionResult | None,
    file_change_result: GitFileChangeCollectionResult | None,
    tag_result: GitTagCollectionResult | None,
    reflog_result: GitReflogCollectionResult | None,
    commit_timestamps: tuple[TimestampKind, ...],
    file_change_timestamps: tuple[TimestampKind, ...],
) -> CoverageFragment:
    """Build Git-owned discovery, extraction, and pre-delivery accounting."""

    records: list[RecordCoverage] = []
    timestamps: list[CollectionTimestampCoverage] = []

    if commit_result is not None and commit_timestamps:
        if commit_result.repository_accounting:
            for accounting in commit_result.repository_accounting:
                _append_partition(
                    records,
                    timestamps,
                    target=os.fspath(accounting.repository.root),
                    record_kind=RecordKind.COMMIT,
                    discovered=accounting.discovered_commit_ids,
                    eligible=accounting.eligible_commits,
                    record_errors=accounting.record_errors,
                    timestamp_kinds=commit_timestamps,
                    examined={kind: accounting.timestamp_value_count(_git_role(kind)) for kind in commit_timestamps},
                    values_read={
                        kind: accounting.timestamp_value_count(_git_role(kind)) for kind in commit_timestamps
                    },
                    unavailable={},
                    scope_matches={kind: accounting.scope_match_count(_git_role(kind)) for kind in commit_timestamps},
                    scope_errors={
                        kind: accounting.scope_evaluation_error_count(_git_role(kind)) for kind in commit_timestamps
                    },
                    materialization_errors={
                        kind: accounting.materialization_error_count(_git_role(kind)) for kind in commit_timestamps
                    },
                )
        else:
            if commit_result.discovered_commit_ids or commit_result.commits:
                raise RuntimeError("non-empty Git commit results require repository accounting")
            _append_partition(
                records,
                timestamps,
                target=GIT_COVERAGE_TARGET,
                record_kind=RecordKind.COMMIT,
                discovered=0,
                eligible=0,
                record_errors=0,
                timestamp_kinds=commit_timestamps,
                examined={},
                values_read={},
                unavailable={},
                scope_matches={},
                materialization_errors={},
            )

    if file_change_result is not None:
        if file_change_result.repository_accounting:
            for accounting in file_change_result.repository_accounting:
                _append_partition(
                    records,
                    timestamps,
                    target=os.fspath(accounting.repository.root),
                    record_kind=RecordKind.GIT_FILE_CHANGE,
                    discovered=accounting.discovered_changes,
                    eligible=accounting.discovered_changes,
                    record_errors=0,
                    timestamp_kinds=file_change_timestamps,
                    examined={kind: accounting.timestamp_value_count(kind) for kind in file_change_timestamps},
                    values_read={kind: accounting.timestamp_value_count(kind) for kind in file_change_timestamps},
                    unavailable={},
                    scope_matches={kind: accounting.scope_match_count(kind) for kind in file_change_timestamps},
                    materialization_errors={},
                )
        else:
            if file_change_result.discovered_changes or file_change_result.changes:
                raise RuntimeError("non-empty Git file-change results require repository accounting")
            _append_partition(
                records,
                timestamps,
                target=GIT_COVERAGE_TARGET,
                record_kind=RecordKind.GIT_FILE_CHANGE,
                discovered=0,
                eligible=0,
                record_errors=0,
                timestamp_kinds=file_change_timestamps,
                examined={},
                values_read={},
                unavailable={},
                scope_matches={},
                materialization_errors={},
            )

    if tag_result is not None:
        if tag_result.repository_accounting:
            for accounting in tag_result.repository_accounting:
                _append_partition(
                    records,
                    timestamps,
                    target=os.fspath(accounting.repository.root),
                    record_kind=RecordKind.TAG,
                    discovered=accounting.discovered_tags,
                    eligible=accounting.captured_tags,
                    record_errors=accounting.record_errors,
                    timestamp_kinds=(TimestampKind.GIT_TAGGER,),
                    examined={TimestampKind.GIT_TAGGER: accounting.captured_tags},
                    values_read={TimestampKind.GIT_TAGGER: accounting.captured_tagger_timestamps},
                    unavailable={TimestampKind.GIT_TAGGER: accounting.unavailable_tagger_timestamps},
                    scope_matches={TimestampKind.GIT_TAGGER: accounting.scope_matches},
                    materialization_errors={},
                )
        else:
            if tag_result.discovered_tags or tag_result.tags:
                raise RuntimeError("non-empty Git tag results require repository accounting")
            _append_partition(
                records,
                timestamps,
                target=GIT_COVERAGE_TARGET,
                record_kind=RecordKind.TAG,
                discovered=0,
                eligible=0,
                record_errors=0,
                timestamp_kinds=(TimestampKind.GIT_TAGGER,),
                examined={},
                values_read={},
                unavailable={},
                scope_matches={},
                materialization_errors={},
            )

    if reflog_result is not None:
        targets = tuple(
            dict.fromkeys(
                os.fspath(item.repository.root)
                for item in (
                    *reflog_result.entries,
                    *reflog_result.available_refs,
                    *reflog_result.refs_without_reflog,
                )
            )
        )
        for target in targets or (GIT_COVERAGE_TARGET,):
            entries = tuple(item for item in reflog_result.entries if os.fspath(item.repository.root) == target)
            statuses = tuple(
                item for item in reflog_result.available_refs if os.fspath(item.repository.root) == target
            )
            captured_entries = sum(item.captured_entry_count for item in statuses) if statuses else len(entries)
            unavailable_entries = sum(item.unavailable_entry_count for item in statuses)
            _append_partition(
                records,
                timestamps,
                target=target,
                record_kind=RecordKind.REFLOG,
                discovered=captured_entries + unavailable_entries,
                eligible=captured_entries,
                record_errors=unavailable_entries,
                timestamp_kinds=(TimestampKind.GIT_REFLOG,),
                examined={TimestampKind.GIT_REFLOG: captured_entries},
                values_read={TimestampKind.GIT_REFLOG: captured_entries},
                unavailable={},
                scope_matches={TimestampKind.GIT_REFLOG: sum(item.scope_match_count for item in statuses)},
                materialization_errors={},
            )

    return CoverageFragment(tuple(records), tuple(timestamps))


def _append_partition(
    records: list[RecordCoverage],
    timestamps: list[CollectionTimestampCoverage],
    *,
    target: str,
    record_kind: RecordKind,
    discovered: int,
    eligible: int,
    record_errors: int,
    timestamp_kinds: tuple[TimestampKind, ...],
    examined: Mapping[TimestampKind, int],
    values_read: Mapping[TimestampKind, int],
    unavailable: Mapping[TimestampKind, int],
    scope_matches: Mapping[TimestampKind, int],
    materialization_errors: Mapping[TimestampKind, int],
    scope_errors: Mapping[TimestampKind, int] | None = None,
) -> None:
    if discovered != eligible + record_errors:
        raise RuntimeError(f"{record_kind.value} record accounting does not reconcile")
    records.append(
        RecordCoverage(
            RecordCoverageKey(Source.GIT, target, record_kind),
            discovered=discovered,
            eligible=eligible,
            record_errors=record_errors,
        )
    )
    for kind in timestamp_kinds:
        values_read_count = values_read.get(kind, 0)
        unavailable_count = unavailable.get(kind, 0)
        examined_count = examined.get(kind, 0)
        extraction_errors = examined_count - values_read_count - unavailable_count
        if extraction_errors < 0:
            raise RuntimeError(f"{record_kind.value}/{kind.value} extraction accounting is negative")
        timestamps.append(
            CollectionTimestampCoverage(
                key=TimestampCoverageKey(Source.GIT, target, record_kind, kind),
                examined=examined_count,
                values_read=values_read_count,
                unavailable=unavailable_count,
                extraction_errors=extraction_errors,
                scope_matches=scope_matches.get(kind, 0),
                materialization_errors=materialization_errors.get(kind, 0),
                scope_errors=(scope_errors or {}).get(kind, 0),
            )
        )


def _git_role(kind: TimestampKind) -> GitSignatureRole:
    if kind is TimestampKind.GIT_AUTHOR:
        return "author"
    if kind is TimestampKind.GIT_COMMITTER:
        return "committer"
    raise ValueError(f"commit coverage does not support {kind.value}")


__all__ = ["GIT_COVERAGE_TARGET", "build_git_coverage_fragment"]
