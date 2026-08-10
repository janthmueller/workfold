"""Reconcile collector accounting with pipeline selection and plotting outcomes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar

from workfold.app.collection import Collection
from workfold.app.resolution import git_timestamp_kinds
from workfold.config import RawOptions
from workfold.coverage import (
    CoverageLedger,
    CoverageLedgerBuilder,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverageKey,
    RecordDisposition,
    SelectionDisposition,
    TimestampCoverageKey,
)
from workfold.models import RecordKind, Source, TimestampKind
from workfold.pipeline import PlottingCountKey, SelectionCountKey

GIT_COVERAGE_TARGET = "selected Git repositories"


def build_coverage(
    collection: Collection,
    options: RawOptions,
    *,
    selection: Mapping[SelectionCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
) -> CoverageLedger:
    """Build a reconciled coverage ledger for every enabled collector partition."""

    ledgers: list[CoverageLedger] = []
    timestamp_kinds = git_timestamp_kinds(options.git_date)

    if collection.commit_result is not None and options.git_mode.includes_commit_markers:
        result = collection.commit_result
        if result.repository_accounting:
            for accounting in result.repository_accounting:
                target = os.fspath(accounting.repository.root)
                ledgers.append(
                    _build_partition_coverage(
                        target=target,
                        record_kind=RecordKind.COMMIT,
                        discovered=accounting.discovered_commit_ids,
                        eligible=accounting.captured_commits,
                        record_errors=accounting.record_errors,
                        timestamp_kinds=timestamp_kinds,
                        captured={kind: accounting.captured_commits for kind in timestamp_kinds},
                        unavailable={},
                        selection=selection,
                        plotting=plotting,
                    )
                )
        else:
            ledgers.append(
                _build_partition_coverage(
                    target=GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.COMMIT,
                    discovered=result.discovered_commit_ids,
                    eligible=len(result.commits),
                    record_errors=result.discovered_commit_ids - len(result.commits),
                    timestamp_kinds=timestamp_kinds,
                    captured={kind: len(result.commits) for kind in timestamp_kinds},
                    unavailable={},
                    selection=selection,
                    plotting=plotting,
                )
            )

    if collection.file_change_result is not None:
        result = collection.file_change_result
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
                        captured={kind: accounting.discovered_changes for kind in timestamp_kinds},
                        unavailable={},
                        selection=selection,
                        plotting=plotting,
                    )
                )
        else:
            targets = tuple(dict.fromkeys(os.fspath(item.repository.root) for item in result.changes))
            for target in targets or (GIT_COVERAGE_TARGET,):
                changes = tuple(item for item in result.changes if os.fspath(item.repository.root) == target)
                ledgers.append(
                    _build_partition_coverage(
                        target=target,
                        record_kind=RecordKind.GIT_FILE_CHANGE,
                        discovered=len(changes),
                        eligible=len(changes),
                        record_errors=0,
                        timestamp_kinds=timestamp_kinds,
                        captured={kind: len(changes) for kind in timestamp_kinds},
                        unavailable={},
                        selection=selection,
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
                        captured={TimestampKind.GIT_TAGGER: accounting.captured_tagger_timestamps},
                        unavailable={TimestampKind.GIT_TAGGER: accounting.unavailable_tagger_timestamps},
                        selection=selection,
                        plotting=plotting,
                    )
                )
        else:
            ledgers.append(
                _build_partition_coverage(
                    target=GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.TAG,
                    discovered=result.discovered_tags,
                    eligible=len(result.tags),
                    record_errors=result.discovered_tags - len(result.tags),
                    timestamp_kinds=(TimestampKind.GIT_TAGGER,),
                    captured={TimestampKind.GIT_TAGGER: result.captured_tagger_timestamps},
                    unavailable={TimestampKind.GIT_TAGGER: result.unavailable_tagger_timestamps},
                    selection=selection,
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
                    captured={TimestampKind.GIT_REFLOG: captured_entries},
                    unavailable={},
                    selection=selection,
                    plotting=plotting,
                )
            )

    if collection.filesystem_result is not None:
        ledgers.append(collection.filesystem_result.build_coverage_counts(selection, plotting))

    ledger = CoverageLedger()
    for item in ledgers:
        ledger = ledger.merge(item)
    return ledger


def _build_partition_coverage(
    *,
    target: str,
    record_kind: RecordKind,
    discovered: int,
    eligible: int,
    record_errors: int,
    timestamp_kinds: tuple[TimestampKind, ...],
    captured: Mapping[TimestampKind, int],
    unavailable: Mapping[TimestampKind, int],
    selection: Mapping[SelectionCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
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
        captured_count = captured.get(kind, 0)
        unavailable_count = unavailable.get(kind, 0)
        extraction_errors = eligible - captured_count - unavailable_count
        if extraction_errors < 0:
            raise RuntimeError(f"{record_kind.value}/{kind.value} extraction accounting is negative")
        builder.request_slot(key, eligible)
        builder.extraction_outcome(key, ExtractionDisposition.CAPTURED, captured_count)
        builder.extraction_outcome(key, ExtractionDisposition.UNAVAILABLE, unavailable_count)
        builder.extraction_outcome(key, ExtractionDisposition.ERROR, extraction_errors)
        for disposition in SelectionDisposition:
            builder.selection_outcome(key, disposition, _pipeline_outcome_count(selection, key, disposition))
        for disposition in PlottingDisposition:
            builder.plotting_outcome(key, disposition, _pipeline_outcome_count(plotting, key, disposition))
    return builder.build()


_OutcomeDisposition = TypeVar("_OutcomeDisposition", SelectionDisposition, PlottingDisposition)


def _pipeline_outcome_count(
    counts: Mapping[tuple[TimestampCoverageKey, _OutcomeDisposition], int],
    key: TimestampCoverageKey,
    disposition: _OutcomeDisposition,
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
