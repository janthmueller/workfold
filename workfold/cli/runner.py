"""Public composition root for Workfold's collector-neutral pipeline."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TextIO

from workfold.application.collection import CollectorServices
from workfold.application.execution import execute
from workfold.application.report import ReportRequirements
from workfold.collection.diagnostics import CollectorDiagnostic, DiagnosticSeverity
from workfold.collection.filesystem import FilesystemCollector
from workfold.collection.git import GitCollector, GitRepositoryResolver
from workfold.collection.git.changes import GitFileChangeCollector
from workfold.collection.git.reflogs import GitReflogCollector
from workfold.collection.git.tags import GitTagCollector
from workfold.configuration.options import MarkerStyle, RunOptions
from workfold.reporting.sanitization import sanitize_terminal_text
from workfold.reporting.terminal import TerminalOptions, terminal_color_enabled, write_terminal

_USAGE_COLLECTION_FAILURE_CODES = frozenset({"git_not_found", "not_git_repository", "path_not_found"})


def run(
    options: RunOptions,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    terminal_width: int | None = None,
    collectors: CollectorServices | None = None,
) -> int:
    """Execute one Workfold run and return a process-style exit status."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    environment = os.environ if environ is None else environ
    preferences = options.terminal
    execution = execute(
        options,
        collectors or default_collector_services(),
        ReportRequirements(
            outside_event_limit=preferences.outside_limit if preferences.list_outside else 0,
            retain_git_identities=preferences.marker_style is MarkerStyle.IDENTITY,
        ),
        now=now,
        environ=environ,
    )
    collection = execution.collection
    if execution.report is None:
        _write_diagnostics(collection.diagnostics, errors, strict=preferences.strict)
        return _failed_collection_exit_status(collection.diagnostics)

    width = terminal_width if terminal_width is not None else shutil.get_terminal_size(fallback=(80, 24)).columns
    presentation = TerminalOptions(
        width=max(60, width),
        color=terminal_color_enabled(
            no_color=preferences.no_color,
            environ=environment,
            stdout_is_tty=_is_tty(output),
        ),
        list_outside=preferences.list_outside,
        verbose=preferences.verbose,
        band_label=preferences.band_label,
        show_empty_bands=preferences.show_empty_bands,
        marker_style=preferences.marker_style,
        grid_style=preferences.grid_style,
        coverage=preferences.coverage,
    )
    write_terminal(execution.report, output, options=presentation)
    if collection.diagnostics:
        _write_diagnostics(
            collection.diagnostics,
            errors,
            strict=preferences.strict,
            leading_blank_line=True,
        )
    return 1 if preferences.strict and execution.is_partial else 0


def _write_diagnostics(
    diagnostics: Sequence[CollectorDiagnostic],
    stream: TextIO,
    *,
    strict: bool,
    leading_blank_line: bool = False,
) -> None:
    if diagnostics and leading_blank_line:
        stream.write("\n")
    for diagnostic in diagnostics:
        message = sanitize_terminal_text(diagnostic.message)
        target = sanitize_terminal_text(diagnostic.target)
        severity = DiagnosticSeverity.ERROR if strict and diagnostic.affects_completeness else diagnostic.severity
        stream.write(f"{severity.value}: {message} [{target}]\n")
        if diagnostic.hint:
            stream.write(f"hint: {sanitize_terminal_text(diagnostic.hint)}\n")


def _failed_collection_exit_status(diagnostics: Sequence[CollectorDiagnostic]) -> int:
    """Map a wholly unusable target/dependency selection to its CLI status."""

    error_codes = {item.code for item in diagnostics if item.severity is DiagnosticSeverity.ERROR}
    if error_codes and error_codes <= _USAGE_COLLECTION_FAILURE_CODES:
        return 2
    return 1


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def default_collector_services() -> CollectorServices:
    """Assemble Workfold's production collection adapters."""

    return CollectorServices(
        git=GitCollector(),
        repositories=GitRepositoryResolver(),
        file_changes=GitFileChangeCollector(),
        tags=GitTagCollector(),
        reflogs=GitReflogCollector(),
        filesystem=FilesystemCollector(),
    )


__all__ = ["default_collector_services", "run"]
