"""Public composition root for Workfold's collector-neutral pipeline."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TextIO

from workfold.app.collection import collect
from workfold.app.coverage import build_coverage
from workfold.app.report_context import build_report_context
from workfold.app.resolution import resolve_date_range, resolve_schedule, resolve_timezone_selection
from workfold.collectors.base import CollectorDiagnostic, DiagnosticSeverity
from workfold.collectors.filesystem import FilesystemCollector
from workfold.collectors.git import GitCollector, GitRepositoryResolver
from workfold.collectors.git_changes import GitFileChangeCollector
from workfold.collectors.git_reflogs import GitReflogCollector
from workfold.collectors.git_tags import GitTagCollector
from workfold.config import RawOptions
from workfold.pipeline import ActivityPipeline
from workfold.renderers.terminal import TerminalOptions, terminal_color_enabled, write_terminal
from workfold.reports import build_report
from workfold.sanitization import sanitize_terminal_text

_USAGE_COLLECTION_FAILURE_CODES = frozenset({"git_not_found", "not_git_repository", "path_not_found"})


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

    timezone_value = resolve_timezone_selection(options, environment)
    selected_range, range_label = resolve_date_range(options, timezone_value, clock_value)
    schedule = resolve_schedule(options)
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
    collection = collect(
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
    ledger = build_coverage(
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
        build_report_context(
            collection,
            options,
            ledger,
            range_label=range_label,
            timezone_label=timezone_value.key,
            schedule_label=str(schedule),
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


def _write_diagnostics(diagnostics: Sequence[CollectorDiagnostic], stream: TextIO) -> None:
    for diagnostic in diagnostics:
        message = sanitize_terminal_text(diagnostic.message)
        target = sanitize_terminal_text(diagnostic.target)
        stream.write(f"workfold: {diagnostic.severity.value}: {message} [{target}]\n")
        if diagnostic.hint:
            stream.write(f"workfold: hint: {sanitize_terminal_text(diagnostic.hint)}\n")


def _failed_collection_exit_status(diagnostics: Sequence[CollectorDiagnostic]) -> int:
    """Map a wholly unusable target/dependency selection to its CLI status."""

    error_codes = {item.code for item in diagnostics if item.severity is DiagnosticSeverity.ERROR}
    if error_codes and error_codes <= _USAGE_COLLECTION_FAILURE_CODES:
        return 2
    return 1


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


__all__ = ["run"]
