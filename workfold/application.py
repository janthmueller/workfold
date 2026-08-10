"""Application orchestration for Workfold's collector-neutral pipeline."""

from __future__ import annotations

import os
import shutil
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TextIO, TypeVar
from zoneinfo import ZoneInfo

from workfold.collectors.base import CollectorDiagnostic, DiagnosticSeverity
from workfold.collectors.filesystem import FilesystemCollectionResult, FilesystemCollector
from workfold.collectors.git import (
    CollectedGitCommit,
    GitCollectionResult,
    GitCollector,
    GitRepositoryResolutionResult,
    GitRepositoryResolver,
)
from workfold.collectors.git_changes import (
    CollectedGitFileChange,
    GitFileChangeCollectionResult,
    GitFileChangeCollector,
    GitFileChangeRepositoryAccounting,
)
from workfold.collectors.git_reflogs import (
    CollectedGitReflog,
    GitReflogCollectionResult,
    GitReflogCollector,
)
from workfold.collectors.git_tags import CollectedGitTag, GitTagCollectionResult, GitTagCollector
from workfold.config import (
    FilesystemEntry,
    FilesystemTime,
    GitDateMode,
    RawOptions,
    RefScope,
    UsageError,
)
from workfold.coverage import (
    Capability,
    CapabilityStatus,
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
from workfold.pipeline import ActivityPipeline, ObservationConsumer, PlottingCountKey, SelectionCountKey
from workfold.renderers.terminal import TerminalOptions, terminal_color_enabled, write_terminal
from workfold.reports import COMPLETE_COVERAGE_STATUS, ReportContext, build_report
from workfold.sanitization import sanitize_terminal_text
from workfold.schedule import Schedule, parse_schedule
from workfold.time_ranges import (
    InstantRangeUnion,
    TimeRangeError,
    all_time_range,
    calendar_date_range,
    current_week_range,
    iso_week_union,
    resolve_local_timezone,
    resolve_timezone,
)

_GIT_COVERAGE_TARGET = "selected Git repositories"
_USAGE_COLLECTION_FAILURE_CODES = frozenset({"git_not_found", "not_git_repository", "path_not_found"})
_ScopeValue = TypeVar("_ScopeValue", Source, RecordKind, TimestampKind)


@dataclass(frozen=True, slots=True)
class _Collection:
    diagnostics: tuple[CollectorDiagnostic, ...]
    capabilities: tuple[Capability, ...]
    any_collector_succeeded: bool
    commit_result: GitCollectionResult | None = None
    file_change_result: GitFileChangeCollectionResult | None = None
    tag_result: GitTagCollectionResult | None = None
    reflog_result: GitReflogCollectionResult | None = None
    filesystem_result: FilesystemCollectionResult | None = None
    repository_resolution: GitRepositoryResolutionResult | None = None


def run(
    options: RawOptions,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    terminal_width: int | None = None,
    git_collector: GitCollector | None = None,
    repository_resolver: GitRepositoryResolver | None = None,
    file_change_collector: GitFileChangeCollector | None = None,
    tag_collector: GitTagCollector | None = None,
    reflog_collector: GitReflogCollector | None = None,
    filesystem_collector: FilesystemCollector | None = None,
) -> int:
    """Execute one Workfold run and return a process-style exit status."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    environment = os.environ if environ is None else environ
    clock_value = datetime.now(timezone.utc) if now is None else now

    timezone_value = _resolve_timezone(options, environment)
    selected_range, range_label = _resolve_date_range(options, timezone_value, clock_value)
    schedule = _resolve_schedule(options)
    display_range = (
        (options.display_hours.start_minute, options.display_hours.end_minute)
        if options.display_hours is not None
        else None
    )
    pipeline = ActivityPipeline(
        selected_range=selected_range,
        identity_filters=options.git_identities,
        timezone_value=timezone_value,
        schedule=schedule,
        cluster_window=options.cluster_window,
        display_range=display_range,
        outside_limit=options.limit if options.list_outside else 0,
    )
    collection = _collect(
        options,
        observation_consumer=pipeline.consume,
        git_collector=git_collector,
        repository_resolver=repository_resolver,
        file_change_collector=file_change_collector,
        tag_collector=tag_collector,
        reflog_collector=reflog_collector,
        filesystem_collector=filesystem_collector,
    )
    if not collection.any_collector_succeeded:
        _write_diagnostics(collection.diagnostics, errors)
        return _failed_collection_exit_status(collection.diagnostics)

    aggregation = pipeline.build()
    ledger = _build_coverage(
        collection,
        options,
        selection=pipeline.selection_counts,
        plotting=pipeline.plotting_counts,
    )
    if ledger.markers_plotted != aggregation.event_count:
        raise RuntimeError("coverage marker totals do not match the classified marker stream")

    error_diagnostics = tuple(item for item in collection.diagnostics if item.severity is DiagnosticSeverity.ERROR)
    report = build_report(
        aggregation,
        ReportContext(
            source_label=_source_label(options),
            range_label=range_label,
            timezone_label=timezone_value.key,
            schedule_label=str(schedule),
            coverage_status=_coverage_status_label(collection, ledger, options),
            profile_label=options.profile.value,
            extent_label=_extent_label(collection, options),
            enabled_sources=_enabled_sources(options),
            enabled_record_kinds=_enabled_record_kinds(options),
            identity_label=_identity_label(options),
            ignore_label=_ignore_label(options, collection),
            exclusions=options.exclusions,
            coverage_details=(
                _coverage_details(ledger, collection, options) if options.coverage or options.verbose else ()
            ),
        ),
    )

    width = terminal_width if terminal_width is not None else shutil.get_terminal_size(fallback=(80, 24)).columns
    presentation = TerminalOptions(
        width=max(60, width),
        color=terminal_color_enabled(
            no_color=options.no_color,
            environ=environment,
            stdout_is_tty=_is_tty(output),
        ),
        list_outside=options.list_outside,
        verbose=options.verbose,
    )
    write_terminal(report, output, options=presentation)
    if collection.diagnostics:
        _write_diagnostics(collection.diagnostics, errors)
    is_partial = bool(error_diagnostics) or ledger.has_operational_errors
    return 1 if options.strict and is_partial else 0


def _collect(
    options: RawOptions,
    *,
    observation_consumer: ObservationConsumer,
    git_collector: GitCollector | None,
    repository_resolver: GitRepositoryResolver | None,
    file_change_collector: GitFileChangeCollector | None,
    tag_collector: GitTagCollector | None,
    reflog_collector: GitReflogCollector | None,
    filesystem_collector: FilesystemCollector | None,
) -> _Collection:
    diagnostics: list[CollectorDiagnostic] = []
    capabilities: list[Capability] = []
    any_succeeded = False
    commit_result: GitCollectionResult | None = None
    file_result: GitFileChangeCollectionResult | None = None
    tag_result: GitTagCollectionResult | None = None
    reflog_result: GitReflogCollectionResult | None = None
    filesystem_result: FilesystemCollectionResult | None = None
    repository_resolution: GitRepositoryResolutionResult | None = None

    if options.source.includes_git:
        if options.git_records.includes_commits:
            timestamp_kinds = _git_timestamp_kinds(options.git_date)
            file_results: list[GitFileChangeCollectionResult] = []
            resolved_file_change_collector = file_change_collector or GitFileChangeCollector()

            def consume_file_changes(changes: tuple[CollectedGitFileChange, ...]) -> None:
                for item in changes:
                    observation_consumer(tuple(item.to_observation(kind) for kind in timestamp_kinds))

            def consume_commits(commits: tuple[CollectedGitCommit, ...]) -> None:
                if options.git_mode.includes_commit_markers:
                    for item in commits:
                        observation_consumer(tuple(item.to_observation(kind) for kind in timestamp_kinds))
                if options.git_mode.includes_file_changes:
                    file_results.append(
                        resolved_file_change_collector.collect(
                            commits,
                            change_consumer=consume_file_changes,
                            retain_changes=False,
                        )
                    )

            commit_result = (git_collector or GitCollector()).collect(
                options.paths,
                ref_scope=options.ref_scope,
                commit_consumer=consume_commits,
                retain_commits=False,
            )
            repositories = commit_result.repositories
            diagnostics.extend(commit_result.diagnostics)
            any_succeeded |= commit_result.successful_repositories > 0 or commit_result.discovered_commit_ids > 0
            if options.git_mode.includes_file_changes:
                file_result = _merge_file_change_results(file_results)
                diagnostics.extend(file_result.diagnostics)
        else:
            repository_resolution = (repository_resolver or GitRepositoryResolver()).resolve(options.paths)
            repositories = repository_resolution.repositories
            diagnostics.extend(repository_resolution.diagnostics)

        if options.git_records.includes_tags:

            def consume_tags(tags: tuple[CollectedGitTag, ...]) -> None:
                for item in tags:
                    if item.tagger is not None:
                        observation_consumer((item.to_observation(),))

            tag_result = (tag_collector or GitTagCollector()).collect(
                repositories,
                tag_consumer=consume_tags,
                retain_tags=False,
            )
            diagnostics.extend(tag_result.diagnostics)
            any_succeeded |= tag_result.successful_repositories > 0 or tag_result.discovered_tags > 0
        if options.git_records.includes_reflogs:

            def consume_reflogs(entries: tuple[CollectedGitReflog, ...]) -> None:
                for item in entries:
                    observation_consumer((item.to_observation(),))

            reflog_result = (reflog_collector or GitReflogCollector()).collect(
                repositories,
                entry_consumer=consume_reflogs,
                retain_entries=False,
            )
            diagnostics.extend(reflog_result.diagnostics)
            any_succeeded |= reflog_result.successful_repositories > 0 or reflog_result.discovered_refs > 0

    if options.source.includes_filesystem:
        try:
            filesystem_result = (filesystem_collector or FilesystemCollector()).collect(
                options.paths,
                timestamp_kinds=_filesystem_timestamp_kinds(options.filesystem_times),
                include_regular_files=FilesystemEntry.FILE in options.filesystem_entries,
                include_directories=FilesystemEntry.DIRECTORY in options.filesystem_entries,
                include_symlinks=FilesystemEntry.SYMLINK in options.filesystem_entries,
                respect_gitignore=options.respect_gitignore,
                include_ignored=options.include_ignored,
                exclusions=options.exclusions,
                observation_consumer=observation_consumer,
                retain_entries=False,
                retain_observations=False,
            )
        except ValueError as error:
            raise UsageError(str(error)) from error
        diagnostics.extend(filesystem_result.diagnostics)
        capabilities.extend(filesystem_result.capabilities)
        any_succeeded |= bool(filesystem_result.successful_roots)

    return _Collection(
        diagnostics=tuple(diagnostics),
        capabilities=tuple(capabilities),
        any_collector_succeeded=any_succeeded,
        commit_result=commit_result,
        file_change_result=file_result,
        tag_result=tag_result,
        reflog_result=reflog_result,
        filesystem_result=filesystem_result,
        repository_resolution=repository_resolution,
    )


def _merge_file_change_results(
    results: Sequence[GitFileChangeCollectionResult],
) -> GitFileChangeCollectionResult:
    """Merge bounded Git derivation batches without reconstructing records."""

    accounting_by_repository: dict[str, GitFileChangeRepositoryAccounting] = {}
    for result in results:
        for item in result.repository_accounting:
            existing = accounting_by_repository.get(item.repository.identity)
            if existing is None:
                accounting_by_repository[item.repository.identity] = item
            else:
                accounting_by_repository[item.repository.identity] = GitFileChangeRepositoryAccounting(
                    repository=existing.repository,
                    requested_commits=existing.requested_commits + item.requested_commits,
                    successful_commits=existing.successful_commits + item.successful_commits,
                    parse_errors=existing.parse_errors + item.parse_errors,
                    subprocess_errors=existing.subprocess_errors + item.subprocess_errors,
                    discovered_changes=existing.discovered_changes + item.discovered_changes,
                )
    return GitFileChangeCollectionResult(
        changes=tuple(change for result in results for change in result.changes),
        diagnostics=tuple(diagnostic for result in results for diagnostic in result.diagnostics),
        requested_commits=sum(result.requested_commits for result in results),
        successful_commits=sum(result.successful_commits for result in results),
        discovered_changes=sum(result.discovered_changes for result in results),
        parse_errors=sum(result.parse_errors for result in results),
        subprocess_errors=sum(result.subprocess_errors for result in results),
        repository_accounting=tuple(accounting_by_repository.values()),
        records_retained=all(result.records_retained for result in results),
    )


def _resolve_timezone(options: RawOptions, environ: Mapping[str, str]) -> ZoneInfo:
    try:
        if options.timezone_name is not None:
            return resolve_timezone(options.timezone_name)
        return resolve_local_timezone(environ=environ)
    except TimeRangeError as error:
        raise UsageError(str(error)) from error


def _resolve_date_range(
    options: RawOptions,
    timezone_value: ZoneInfo,
    now: datetime,
) -> tuple[InstantRangeUnion, str]:
    try:
        if options.weeks:
            return iso_week_union(options.weeks, timezone_value), ", ".join(options.weeks)
        if options.from_date is not None or options.to_date is not None:
            label = _calendar_range_label(options.from_date, options.to_date)
            return calendar_date_range(options.from_date, options.to_date, timezone_value), label
        if options.all_dates:
            return all_time_range(), "all available dates"
        local_now = now.astimezone(timezone_value)
        iso = local_now.isocalendar()
        return current_week_range(now, timezone_value), f"{iso.year:04d}-W{iso.week:02d}"
    except (TimeRangeError, ValueError) as error:
        raise UsageError(str(error)) from error


def _resolve_schedule(options: RawOptions) -> Schedule:
    try:
        return parse_schedule(options.hours)
    except ValueError as error:
        raise UsageError(str(error)) from error


def _git_timestamp_kinds(mode: GitDateMode) -> tuple[TimestampKind, ...]:
    if mode is GitDateMode.AUTHOR:
        return (TimestampKind.GIT_AUTHOR,)
    if mode is GitDateMode.COMMITTER:
        return (TimestampKind.GIT_COMMITTER,)
    return (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER)


def _filesystem_timestamp_kinds(values: tuple[FilesystemTime, ...]) -> tuple[TimestampKind, ...]:
    mapping = {
        FilesystemTime.CREATED: TimestampKind.FS_CREATED,
        FilesystemTime.MODIFIED: TimestampKind.FS_MODIFIED,
        FilesystemTime.CHANGED: TimestampKind.FS_METADATA_CHANGED,
        FilesystemTime.ACCESSED: TimestampKind.FS_ACCESSED,
    }
    return tuple(mapping[value] for value in values)


def _build_coverage(
    collection: _Collection,
    options: RawOptions,
    *,
    selection: Mapping[SelectionCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
) -> CoverageLedger:
    ledgers: list[CoverageLedger] = []
    timestamp_kinds = _git_timestamp_kinds(options.git_date)

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
                    target=_GIT_COVERAGE_TARGET,
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
            for target in targets or (_GIT_COVERAGE_TARGET,):
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
                        captured={
                            TimestampKind.GIT_TAGGER: accounting.captured_tagger_timestamps,
                        },
                        unavailable={
                            TimestampKind.GIT_TAGGER: accounting.unavailable_tagger_timestamps,
                        },
                        selection=selection,
                        plotting=plotting,
                    )
                )
        else:
            ledgers.append(
                _build_partition_coverage(
                    target=_GIT_COVERAGE_TARGET,
                    record_kind=RecordKind.TAG,
                    discovered=result.discovered_tags,
                    eligible=len(result.tags),
                    record_errors=result.discovered_tags - len(result.tags),
                    timestamp_kinds=(TimestampKind.GIT_TAGGER,),
                    captured={TimestampKind.GIT_TAGGER: result.captured_tagger_timestamps},
                    unavailable={
                        TimestampKind.GIT_TAGGER: result.unavailable_tagger_timestamps,
                    },
                    selection=selection,
                    plotting=plotting,
                )
            )

    if collection.reflog_result is not None:
        result = collection.reflog_result
        targets = tuple(
            dict.fromkeys(
                os.fspath(item.repository.root)
                for item in (
                    *result.entries,
                    *result.available_refs,
                    *result.refs_without_reflog,
                )
            )
        )
        for target in targets or (_GIT_COVERAGE_TARGET,):
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
            builder.selection_outcome(
                key,
                disposition,
                _pipeline_outcome_count(selection, key, disposition),
            )
        for disposition in PlottingDisposition:
            builder.plotting_outcome(
                key,
                disposition,
                _pipeline_outcome_count(plotting, key, disposition),
            )
    return builder.build()


_OutcomeDisposition = TypeVar("_OutcomeDisposition", SelectionDisposition, PlottingDisposition)


def _pipeline_outcome_count(
    counts: Mapping[tuple[TimestampCoverageKey, _OutcomeDisposition], int],
    key: TimestampCoverageKey,
    disposition: _OutcomeDisposition,
) -> int:
    if key.target != _GIT_COVERAGE_TARGET:
        return counts.get((key, disposition), 0)
    return sum(
        count
        for (candidate, candidate_disposition), count in counts.items()
        if candidate.source is key.source
        and candidate.record_kind is key.record_kind
        and candidate.timestamp_kind is key.timestamp_kind
        and candidate_disposition is disposition
    )


def _calendar_range_label(start: date | None, end: date | None) -> str:
    if start is None:
        return f"through {end}"
    if end is None:
        return f"from {start}"
    return f"{start}..{end}"


def _source_label(options: RawOptions) -> str:
    parts: list[str] = []
    if options.source.includes_git:
        records: list[str] = []
        if options.git_records.includes_commits:
            if options.git_mode.includes_commit_markers:
                records.append("commits")
            if options.git_mode.includes_file_changes:
                records.append("file changes")
        if options.git_records.includes_tags:
            records.append("tags")
        if options.git_records.includes_reflogs:
            records.append("reflogs")
        roles = {
            GitDateMode.AUTHOR: "author dates",
            GitDateMode.COMMITTER: "committer dates",
            GitDateMode.BOTH: "author + committer dates",
        }[options.git_date]
        reachability = {
            RefScope.HEAD: "HEAD",
            RefScope.LOCAL_BRANCHES: "local branches + detached HEAD",
            RefScope.ALL_REFS: "all locally stored refs",
        }[options.ref_scope]
        suffix = f", {roles}, commits from {reachability}" if options.git_records.includes_commits else ""
        parts.append(f"Git {' + '.join(records)}{suffix}")
    if options.source.includes_filesystem:
        time_names = {
            FilesystemTime.CREATED: "birth",
            FilesystemTime.MODIFIED: "modified",
            FilesystemTime.CHANGED: "metadata-changed",
            FilesystemTime.ACCESSED: "accessed",
        }
        times = ",".join(time_names[item] for item in options.filesystem_times)
        reliability = "; atime potentially unreliable" if FilesystemTime.ACCESSED in options.filesystem_times else ""
        parts.append(f"filesystem ({times}{reliability})")
    return "; ".join(parts)


def _extent_label(collection: _Collection, options: RawOptions) -> str | None:
    parts: list[str] = []
    if options.source.includes_git:
        repositories = (
            collection.commit_result.repositories
            if collection.commit_result is not None
            else (collection.repository_resolution.repositories if collection.repository_resolution is not None else ())
        )
        roots = tuple(dict.fromkeys(os.fspath(item.root) for item in repositories))
        if roots:
            parts.append("whole Git repositories=" + ", ".join(roots))
    if options.source.includes_filesystem and collection.filesystem_result is not None:
        roots = tuple(os.fspath(item) for item in collection.filesystem_result.scan_roots)
        if roots:
            parts.append("exact filesystem roots=" + ", ".join(roots))
    return "; ".join(parts) or None


def _enabled_sources(options: RawOptions) -> tuple[Source, ...]:
    sources: list[Source] = []
    if options.source.includes_git:
        sources.append(Source.GIT)
    if options.source.includes_filesystem:
        sources.append(Source.FILESYSTEM)
    return tuple(sources)


def _enabled_record_kinds(options: RawOptions) -> tuple[RecordKind, ...]:
    kinds: list[RecordKind] = []
    if options.source.includes_git:
        if options.git_records.includes_commits and options.git_mode.includes_commit_markers:
            kinds.append(RecordKind.COMMIT)
        if options.git_records.includes_commits and options.git_mode.includes_file_changes:
            kinds.append(RecordKind.GIT_FILE_CHANGE)
        if options.git_records.includes_tags:
            kinds.append(RecordKind.TAG)
        if options.git_records.includes_reflogs:
            kinds.append(RecordKind.REFLOG)
    if options.source.includes_filesystem:
        kinds.append(RecordKind.FILESYSTEM_ENTRY)
    return tuple(kinds)


def _identity_label(options: RawOptions) -> str | None:
    if not options.source.includes_git:
        return None
    if not options.git_identities:
        return "all recorded identities"
    filters = " OR ".join(options.git_identities)
    suffix = "; filesystem unaffected" if options.source.includes_filesystem else ""
    return f"{filters}{suffix}"


def _ignore_label(options: RawOptions, collection: _Collection) -> str | None:
    if not options.source.includes_filesystem:
        return None
    entry_names = {
        FilesystemEntry.FILE: "files",
        FilesystemEntry.DIRECTORY: "directories",
        FilesystemEntry.SYMLINK: "symlinks",
    }
    entry_scope = " + ".join(entry_names[item] for item in options.filesystem_entries)
    if options.include_ignored:
        policy = "ignored entries included"
    else:
        capabilities = tuple(item for item in collection.capabilities if item.name == "standard Git ignore semantics")
        notes = tuple(item.note or "" for item in capabilities)
        if capabilities and all("outside a Git worktree" in note for note in notes):
            policy = "outside a Git worktree; no Git ignore rules apply"
        elif capabilities and any(item.status is CapabilityStatus.UNAVAILABLE for item in capabilities):
            policy = "standard Git ignore policy partially unavailable"
        elif capabilities and any("outside a Git worktree" in note for note in notes):
            policy = "standard Git ignores respected where applicable"
        else:
            policy = "standard Git ignores respected"
    return f"{policy}; {entry_scope}"


def _coverage_status_label(
    collection: _Collection,
    ledger: CoverageLedger,
    options: RawOptions,
) -> str:
    error_count = sum(item.severity is DiagnosticSeverity.ERROR for item in collection.diagnostics)
    if error_count or ledger.has_operational_errors:
        label = f"partial ({error_count} collection error(s))"
    else:
        label = COMPLETE_COVERAGE_STATUS
    qualifiers: list[str] = []
    if options.git_identities:
        qualifiers.append("Git timestamps explicitly filtered by identity")
    if options.exclusions:
        qualifiers.append("explicit exclusions active")
    unsupported_capabilities: dict[str, str | None] = {}
    for capability in collection.capabilities:
        if capability.status is CapabilityStatus.UNSUPPORTED:
            unsupported_capabilities.setdefault(capability.name, capability.note)
    for name, note in unsupported_capabilities.items():
        qualifier = f"{name} unavailable"
        if note:
            qualifier += f": {note}"
        qualifiers.append(qualifier)
    if qualifiers:
        label += "; " + "; ".join(qualifiers)
    return label


def _coverage_details(
    ledger: CoverageLedger,
    collection: _Collection,
    options: RawOptions,
) -> tuple[str, ...]:
    details: list[str] = [
        f"timestamp slots requested: {ledger.slots_requested:,}",
        f"timestamp observations captured: {ledger.observations_captured:,}",
        f"timestamp observations included: {ledger.observations_included:,}",
        f"activity markers plotted: {ledger.markers_plotted:,}",
        *_coverage_scope_details(options),
    ]
    coalesced_total = sum(item.coalesced_into_markers for item in ledger.timestamps)
    if coalesced_total:
        details.append(f"coalesced for plotting with roles preserved: {coalesced_total:,}")
    record_counts: dict[RecordKind, Counter[str]] = {}
    for item in ledger.records:
        counts = record_counts.setdefault(item.key.record_kind, Counter())
        counts.update(
            discovered=item.discovered,
            eligible=item.eligible,
            ignored=item.ignored,
            explicitly_excluded=item.explicitly_excluded,
            excluded_entry_type=item.excluded_entry_type,
            semantic_git_admin=item.semantic_git_admin,
            record_errors=item.record_errors,
        )
    for kind in sorted(record_counts, key=lambda item: item.value):
        counts = record_counts[kind]
        line = f"{_record_label(kind)} discovered: {counts['discovered']:,}"
        outcomes = (
            ("eligible", "eligible"),
            ("ignored", "ignored"),
            ("explicitly_excluded", "explicitly excluded"),
            ("excluded_entry_type", "entry type excluded"),
            ("semantic_git_admin", "Git admin excluded"),
            ("record_errors", "record errors"),
        )
        extras = [f"{label}={counts[name]:,}" for name, label in outcomes if counts[name]]
        details.append(line + ("; " + ", ".join(extras) if extras else ""))

    timestamp_counts: dict[TimestampKind, Counter[str]] = {}
    for item in ledger.timestamps:
        counts = timestamp_counts.setdefault(item.key.timestamp_kind, Counter())
        counts.update(
            requested=item.requested,
            captured=item.captured,
            included=item.included,
            markers=item.markers,
            unavailable=item.unavailable,
            unsupported=item.unsupported,
            errors=item.extraction_errors,
            outside_date=item.outside_date,
            identity_filtered=item.identity_filtered,
            coalesced=item.coalesced_into_markers,
        )
    for kind in sorted(timestamp_counts, key=lambda item: item.value):
        counts = timestamp_counts[kind]
        line = f"{_timestamp_label(kind)} captured: {counts['captured']:,}"
        extras = [
            f"{name.replace('_', ' ')}={counts[name]:,}"
            for name in (
                "requested",
                "included",
                "markers",
                "unavailable",
                "unsupported",
                "errors",
                "outside_date",
                "identity_filtered",
                "coalesced",
            )
            if counts[name]
        ]
        details.append(line + ("; " + ", ".join(extras) if extras else ""))

    if (
        collection.commit_result is not None
        and options.git_mode.includes_file_changes
        and not options.git_mode.includes_commit_markers
    ):
        commit_result = collection.commit_result
        captured_commits = sum(item.captured_commits for item in commit_result.repository_accounting)
        if not commit_result.repository_accounting:
            captured_commits = len(commit_result.commits)
        details.append(
            "Git commit inputs for file-change derivation: "
            f"discovered={commit_result.discovered_commit_ids:,}, "
            f"parsed={captured_commits:,}, "
            f"record errors={commit_result.discovered_commit_ids - captured_commits:,}"
        )
        for accounting in commit_result.repository_accounting:
            details.append(
                f"target Git commit inputs [git] {accounting.repository.root}: "
                f"discovered={accounting.discovered_commit_ids:,}, "
                f"parsed={accounting.captured_commits:,}, "
                f"unavailable={accounting.unavailable_objects:,}, "
                f"parse failures={accounting.parse_errors:,}, "
                f"operational errors={accounting.operational_errors:,}"
            )

    if collection.file_change_result is not None:
        file_result = collection.file_change_result
        details.append(
            "Git file-change derivation: "
            f"commits requested={file_result.requested_commits:,}, "
            f"successfully parsed={file_result.successful_commits:,}, "
            f"parse failures={file_result.parse_errors:,}, "
            f"subprocess failures={file_result.subprocess_errors:,}, "
            f"file changes discovered={file_result.discovered_changes:,}"
        )
        for accounting in file_result.repository_accounting:
            details.append(
                f"target Git file-change derivation [git] {accounting.repository.root}: "
                f"commits requested={accounting.requested_commits:,}, "
                f"successfully parsed={accounting.successful_commits:,}, "
                f"parse failures={accounting.parse_errors:,}, "
                f"subprocess failures={accounting.subprocess_errors:,}, "
                f"file changes discovered={accounting.discovered_changes:,}"
            )

    for item in ledger.records:
        outcomes = (
            f"discovered={item.discovered:,}, eligible={item.eligible:,}, "
            f"ignored={item.ignored:,}, explicitly excluded={item.explicitly_excluded:,}, "
            f"entry type excluded={item.excluded_entry_type:,}, "
            f"Git admin excluded={item.semantic_git_admin:,}, errors={item.record_errors:,}"
        )
        details.append(
            f"target records [{item.key.source.value}] {item.key.target} {item.key.record_kind.value}: {outcomes}"
        )
    for item in ledger.timestamps:
        outcomes = (
            f"requested={item.requested:,}, captured={item.captured:,}, "
            f"unavailable={item.unavailable:,}, unsupported={item.unsupported:,}, "
            f"errors={item.extraction_errors:,}, included={item.included:,}, "
            f"outside date={item.outside_date:,}, identity filtered={item.identity_filtered:,}, "
            f"markers={item.markers:,}, coalesced={item.coalesced_into_markers:,}"
        )
        details.append(
            f"target timestamps [{item.key.source.value}] {item.key.target} "
            f"{item.key.record_kind.value}/{item.key.timestamp_kind.value}: {outcomes}"
        )

    if collection.commit_result is not None and collection.commit_result.duplicate_commit_ids:
        details.append(f"duplicate commit IDs deduplicated: {collection.commit_result.duplicate_commit_ids:,}")
    duplicate_targets = 0
    if collection.commit_result is not None:
        duplicate_targets += collection.commit_result.duplicate_targets
        if collection.commit_result.repository_accounting:
            shared_contexts = len(collection.commit_result.repositories) - len(
                collection.commit_result.repository_accounting
            )
            if shared_contexts > 0:
                details.append(f"linked worktree contexts sharing commit history: {shared_contexts:,}")
    if collection.repository_resolution is not None:
        duplicate_targets += collection.repository_resolution.duplicate_targets
    if duplicate_targets:
        details.append(f"duplicate selected Git targets deduplicated: {duplicate_targets:,}")
    if collection.tag_result is not None:
        details.append(
            f"tags: {collection.tag_result.annotated_tags:,} annotated, "
            f"{collection.tag_result.lightweight_tags:,} lightweight"
        )
    if collection.reflog_result is not None:
        details.append(
            f"reflogs: {len(collection.reflog_result.available_refs):,} available, "
            f"{len(collection.reflog_result.refs_without_reflog):,} unavailable"
        )
    if collection.filesystem_result is not None:
        if collection.filesystem_result.overlapping_roots_deduplicated:
            details.append(
                "overlapping filesystem roots deduplicated: "
                f"{collection.filesystem_result.overlapping_roots_deduplicated:,}"
            )
    for capability in collection.capabilities:
        if capability.status is not CapabilityStatus.SUPPORTED or capability.note:
            note = f" ({capability.note})" if capability.note else ""
            details.append(f"{capability.name}: {capability.status.value}{note}")
    if collection.diagnostics:
        errors = sum(item.severity is DiagnosticSeverity.ERROR for item in collection.diagnostics)
        warnings = len(collection.diagnostics) - errors
        details.append(f"operational diagnostics: {errors:,} error(s), {warnings:,} warning(s)")
    return tuple(details)


def _coverage_scope_details(options: RawOptions) -> tuple[str, ...]:
    requested_sources = set(_enabled_sources(options))
    requested_records = set(_enabled_record_kinds(options))
    requested_timestamps: set[TimestampKind] = set()
    if options.source.includes_git:
        if options.git_records.includes_commits:
            requested_timestamps.update(_git_timestamp_kinds(options.git_date))
        if options.git_records.includes_tags:
            requested_timestamps.add(TimestampKind.GIT_TAGGER)
        if options.git_records.includes_reflogs:
            requested_timestamps.add(TimestampKind.GIT_REFLOG)
    if options.source.includes_filesystem:
        requested_timestamps.update(_filesystem_timestamp_kinds(options.filesystem_times))

    source_names = {Source.GIT: "git", Source.FILESYSTEM: "filesystem"}
    record_names = {
        RecordKind.COMMIT: "commits",
        RecordKind.GIT_FILE_CHANGE: "file changes",
        RecordKind.TAG: "tags",
        RecordKind.REFLOG: "reflogs",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }
    timestamp_names = {kind: _timestamp_label(kind) for kind in TimestampKind}
    return (
        _scope_line("sources", tuple(Source), requested_sources, source_names),
        _scope_line(
            "record kinds",
            tuple(RecordKind),
            requested_records,
            record_names,
        ),
        _scope_line(
            "timestamp kinds",
            tuple(TimestampKind),
            requested_timestamps,
            timestamp_names,
        ),
    )


def _scope_line(
    label: str,
    all_values: Sequence[_ScopeValue],
    requested: AbstractSet[_ScopeValue],
    names: Mapping[_ScopeValue, str],
) -> str:
    requested_text = ", ".join(names[item] for item in all_values if item in requested)
    omitted_text = ", ".join(names[item] for item in all_values if item not in requested)
    return f"scope {label}: requested={requested_text or 'none'}; not requested={omitted_text or 'none'}"


def _record_label(kind: RecordKind) -> str:
    return {
        RecordKind.COMMIT: "Git commits",
        RecordKind.GIT_FILE_CHANGE: "Git file changes",
        RecordKind.TAG: "Git tags",
        RecordKind.REFLOG: "Git reflog entries",
        RecordKind.FILESYSTEM_ENTRY: "filesystem entries",
    }[kind]


def _timestamp_label(kind: TimestampKind) -> str:
    return kind.value.replace("git_", "Git ").replace("fs_", "filesystem ").replace("_", " ")


def _write_diagnostics(diagnostics: Sequence[CollectorDiagnostic], stream: TextIO) -> None:
    for diagnostic in diagnostics:
        message = sanitize_terminal_text(diagnostic.message)
        target = sanitize_terminal_text(diagnostic.target)
        stream.write(f"workfold: {diagnostic.severity.value}: {message} [{target}]\n")
        if diagnostic.hint:
            stream.write(f"workfold: hint: {sanitize_terminal_text(diagnostic.hint)}\n")


def _failed_collection_exit_status(
    diagnostics: Sequence[CollectorDiagnostic],
) -> int:
    """Map a wholly unusable target/dependency selection to its CLI status."""

    error_codes = {item.code for item in diagnostics if item.severity is DiagnosticSeverity.ERROR}
    if error_codes and error_codes <= _USAGE_COLLECTION_FAILURE_CODES:
        return 2
    return 1


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


__all__ = ["run"]
