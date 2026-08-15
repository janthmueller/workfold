"""Combine collector extraction accounting with selected plotting outcomes."""

from __future__ import annotations

import os
from collections.abc import Mapping

from workfold.application.collection import Collection
from workfold.application.collection_plan import CollectionPlan
from workfold.collection.git.objects.models import GitSignatureRole
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import (
    CoverageLedger,
    CoverageLedgerBuilder,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverageKey,
    merge_ledgers,
)
from workfold.domain.observations import RecordKind, Source, TimestampKind
from workfold.folding.pipeline import ObservationCountKey, PlottingCountKey

GIT_COVERAGE_TARGET = "selected Git repositories"


def build_coverage(
    collection: Collection,
    options: RunOptions,
    *,
    observations: Mapping[ObservationCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
) -> CoverageLedger:
    """Build a reconciled coverage ledger for every enabled collector partition."""

    ledgers: list[CoverageLedger] = []
    plan = CollectionPlan.from_selection(options.evidence)

    if collection.commit_result is not None and plan.commit_timestamps:
        result = collection.commit_result
        timestamp_kinds = plan.commit_timestamps
        if result.repository_accounting:
            for accounting in result.repository_accounting:
                target = os.fspath(accounting.repository.root)
                ledgers.append(
                    _build_partition_coverage(
                        target=target,
                        record_kind=RecordKind.COMMIT,
                        discovered=accounting.discovered_commit_ids,
                        eligible=accounting.eligible_commits,
                        record_errors=accounting.record_errors,
                        timestamp_kinds=timestamp_kinds,
                        examined={kind: accounting.timestamp_value_count(_git_role(kind)) for kind in timestamp_kinds},
                        values_read={
                            kind: accounting.timestamp_value_count(_git_role(kind)) for kind in timestamp_kinds
                        },
                        unavailable={},
                        scope_matches={kind: accounting.scope_match_count(_git_role(kind)) for kind in timestamp_kinds},
                        scope_errors={
                            kind: accounting.scope_evaluation_error_count(_git_role(kind)) for kind in timestamp_kinds
                        },
                        materialization_errors={
                            kind: accounting.materialization_error_count(_git_role(kind)) for kind in timestamp_kinds
                        },
                        observations=observations,
                        plotting=plotting,
                    )
                )
        else:
            if result.discovered_commit_ids or result.commits:
                raise RuntimeError("non-empty Git commit results require repository accounting")
            ledgers.append(
                _build_partition_coverage(
                    target=GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.COMMIT,
                    discovered=result.discovered_commit_ids,
                    eligible=len(result.commits),
                    record_errors=result.discovered_commit_ids - len(result.commits),
                    timestamp_kinds=timestamp_kinds,
                    examined={kind: len(result.commits) for kind in timestamp_kinds},
                    values_read={kind: len(result.commits) for kind in timestamp_kinds},
                    unavailable={},
                    scope_matches={},
                    materialization_errors={},
                    observations=observations,
                    plotting=plotting,
                )
            )

    if collection.file_change_result is not None:
        result = collection.file_change_result
        timestamp_kinds = plan.file_change_timestamps
        if result.repository_accounting:
            for accounting in result.repository_accounting:
                target = os.fspath(accounting.repository.root)
                ledgers.append(
                    _build_partition_coverage(
                        target=target,
                        record_kind=RecordKind.GIT_FILE_CHANGE,
                        discovered=accounting.discovered_changes,
                        eligible=accounting.discovered_changes,
                        record_errors=0,
                        timestamp_kinds=timestamp_kinds,
                        examined={kind: accounting.timestamp_value_count(kind) for kind in timestamp_kinds},
                        values_read={kind: accounting.timestamp_value_count(kind) for kind in timestamp_kinds},
                        unavailable={},
                        scope_matches={kind: accounting.scope_match_count(kind) for kind in timestamp_kinds},
                        materialization_errors={},
                        observations=observations,
                        plotting=plotting,
                    )
                )
        else:
            if result.discovered_changes or result.changes:
                raise RuntimeError("non-empty Git file-change results require repository accounting")
            ledgers.append(
                _build_partition_coverage(
                    target=GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.GIT_FILE_CHANGE,
                    discovered=0,
                    eligible=0,
                    record_errors=0,
                    timestamp_kinds=timestamp_kinds,
                    examined={},
                    values_read={},
                    unavailable={},
                    scope_matches={},
                    materialization_errors={},
                    observations=observations,
                    plotting=plotting,
                )
            )

    if collection.tag_result is not None:
        result = collection.tag_result
        if result.repository_accounting:
            for accounting in result.repository_accounting:
                target = os.fspath(accounting.repository.root)
                ledgers.append(
                    _build_partition_coverage(
                        target=target,
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
                        observations=observations,
                        plotting=plotting,
                    )
                )
        else:
            if result.discovered_tags or result.tags:
                raise RuntimeError("non-empty Git tag results require repository accounting")
            ledgers.append(
                _build_partition_coverage(
                    target=GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.TAG,
                    discovered=result.discovered_tags,
                    eligible=len(result.tags),
                    record_errors=result.discovered_tags - len(result.tags),
                    timestamp_kinds=(TimestampKind.GIT_TAGGER,),
                    examined={TimestampKind.GIT_TAGGER: len(result.tags)},
                    values_read={TimestampKind.GIT_TAGGER: result.captured_tagger_timestamps},
                    unavailable={TimestampKind.GIT_TAGGER: result.unavailable_tagger_timestamps},
                    scope_matches={},
                    materialization_errors={},
                    observations=observations,
                    plotting=plotting,
                )
            )

    if collection.reflog_result is not None:
        result = collection.reflog_result
        targets = tuple(
            dict.fromkeys(
                os.fspath(item.repository.root)
                for item in (*result.entries, *result.available_refs, *result.refs_without_reflog)
            )
        )
        for target in targets or (GIT_COVERAGE_TARGET,):
            entries = tuple(item for item in result.entries if os.fspath(item.repository.root) == target)
            statuses = tuple(item for item in result.available_refs if os.fspath(item.repository.root) == target)
            captured_entries = sum(item.captured_entry_count for item in statuses) if statuses else len(entries)
            unavailable_entries = sum(item.unavailable_entry_count for item in statuses)
            ledgers.append(
                _build_partition_coverage(
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
                    observations=observations,
                    plotting=plotting,
                )
            )

    if collection.filesystem_result is not None:
        ledgers.append(collection.filesystem_result.build_coverage_counts(observations, plotting))

    return merge_ledgers(*ledgers)


def _build_partition_coverage(
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
    observations: Mapping[ObservationCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
    scope_errors: Mapping[TimestampKind, int] | None = None,
) -> CoverageLedger:
    if discovered != eligible + record_errors:
        raise RuntimeError(f"{record_kind.value} record accounting does not reconcile")
    builder = CoverageLedgerBuilder()
    record_key = RecordCoverageKey(Source.GIT, target, record_kind)
    builder.discover_record(record_key, discovered)
    builder.record_outcome(record_key, RecordDisposition.ELIGIBLE, eligible)
    builder.record_outcome(record_key, RecordDisposition.RECORD_ERROR, record_errors)

    for kind in timestamp_kinds:
        key = TimestampCoverageKey(Source.GIT, target, record_kind, kind)
        values_read_count = values_read.get(kind, 0)
        unavailable_count = unavailable.get(kind, 0)
        examined_count = examined.get(kind, 0)
        extraction_errors = examined_count - values_read_count - unavailable_count
        if extraction_errors < 0:
            raise RuntimeError(f"{record_kind.value}/{kind.value} extraction accounting is negative")
        builder.examine_slot(key, examined_count)
        builder.extraction_outcome(key, ExtractionDisposition.CAPTURED, values_read_count)
        builder.extraction_outcome(key, ExtractionDisposition.UNAVAILABLE, unavailable_count)
        builder.extraction_outcome(key, ExtractionDisposition.ERROR, extraction_errors)
        builder.match_scope(key, scope_matches.get(kind, 0))
        builder.scope_error(key, (scope_errors or {}).get(kind, 0))
        builder.materialization_error(key, materialization_errors.get(kind, 0))
        builder.select_observation(key, _pipeline_observation_count(observations, key))
        for disposition in PlottingDisposition:
            builder.plotting_outcome(key, disposition, _pipeline_outcome_count(plotting, key, disposition))
    return builder.build()


def _pipeline_outcome_count(
    counts: Mapping[tuple[TimestampCoverageKey, PlottingDisposition], int],
    key: TimestampCoverageKey,
    disposition: PlottingDisposition,
) -> int:
    if key.target != GIT_COVERAGE_TARGET:
        return counts.get((key, disposition), 0)
    return sum(
        count
        for (candidate, candidate_disposition), count in counts.items()
        if candidate.source is key.source
        and candidate.record_kind is key.record_kind
        and candidate.timestamp_kind is key.timestamp_kind
        and candidate_disposition is disposition
    )


def _pipeline_observation_count(
    counts: Mapping[TimestampCoverageKey, int],
    key: TimestampCoverageKey,
) -> int:
    if key.target != GIT_COVERAGE_TARGET:
        return counts.get(key, 0)
    return sum(
        count
        for candidate, count in counts.items()
        if candidate.source is key.source
        and candidate.record_kind is key.record_kind
        and candidate.timestamp_kind is key.timestamp_kind
    )


def _git_role(kind: TimestampKind) -> GitSignatureRole:
    if kind is TimestampKind.GIT_AUTHOR:
        return "author"
    if kind is TimestampKind.GIT_COMMITTER:
        return "committer"
    raise ValueError(f"commit coverage does not support {kind.value}")
