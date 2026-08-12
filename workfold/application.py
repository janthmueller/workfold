"""Public composition root for Workfold's collector-neutral pipeline."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TextIO

from workfold.app.execution import execute
from workfold.collectors.base import CollectorDiagnostic, DiagnosticSeverity
from workfold.collectors.filesystem import FilesystemCollector
from workfold.collectors.git import GitCollector, GitRepositoryResolver
from workfold.collectors.git_changes import GitFileChangeCollector
from workfold.collectors.git_reflogs import GitReflogCollector
from workfold.collectors.git_tags import GitTagCollector
from workfold.config import RawOptions
from workfold.renderers.terminal import TerminalOptions, terminal_color_enabled, write_terminal
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
    execution = execute(
        options,
        now=now,
        environ=environ,
        git_collector=git_collector,
        repository_resolver=repository_resolver,
        file_change_collector=file_change_collector,
        tag_collector=tag_collector,
        reflog_collector=reflog_collector,
        filesystem_collector=filesystem_collector,
    )
    collection = execution.collection
    if execution.report is None:
        _write_diagnostics(collection.diagnostics, errors, strict=options.strict)
        return _failed_collection_exit_status(collection.diagnostics)

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
        band_label=options.band_label,
        show_empty_bands=options.show_empty_bands,
        marker_style=options.marker_style,
        grid_style=options.grid_style,
    )
    write_terminal(execution.report, output, options=presentation)
    if collection.diagnostics:
        _write_diagnostics(
            collection.diagnostics,
            errors,
            strict=options.strict,
            leading_blank_line=True,
        )
    return 1 if options.strict and execution.is_partial else 0


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


__all__ = ["run"]
