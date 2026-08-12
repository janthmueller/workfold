"""Renderer-neutral execution of one Workfold collection and report build."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from workfold.aggregation import AggregationBuilder
from workfold.app.collection import Collection, collect
from workfold.app.coverage import build_coverage
from workfold.app.report_context import build_report_context
from workfold.app.resolution import resolve_date_range, resolve_schedule, resolve_timezone_selection
from workfold.collectors.base import DiagnosticSeverity
from workfold.collectors.filesystem import FilesystemCollector
from workfold.collectors.git import GitCollector, GitRepositoryResolver
from workfold.collectors.git_changes import GitFileChangeCollector
from workfold.collectors.git_reflogs import GitReflogCollector
from workfold.collectors.git_tags import GitTagCollector
from workfold.config import MarkerStyle, RawOptions
from workfold.coverage import CoverageLedger
from workfold.pipeline import ActivityClassifier
from workfold.reports import Report, build_report
from workfold.scope import ObservationScope


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Domain result available to terminal and future report renderers."""

    collection: Collection
    report: Report | None
    coverage: CoverageLedger | None

    @property
    def is_partial(self) -> bool:
        diagnostics_are_partial = any(
            item.occurrence_count(DiagnosticSeverity.ERROR) or item.completeness_failure_count
            for item in self.collection.diagnostics
        )
        return diagnostics_are_partial or bool(self.coverage and self.coverage.has_operational_errors)


def execute(
    options: RawOptions,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    git_collector: GitCollector | None = None,
    repository_resolver: GitRepositoryResolver | None = None,
    file_change_collector: GitFileChangeCollector | None = None,
    tag_collector: GitTagCollector | None = None,
    reflog_collector: GitReflogCollector | None = None,
    filesystem_collector: FilesystemCollector | None = None,
) -> ExecutionResult:
    """Collect, classify, aggregate, and build renderer-neutral report data."""

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
    aggregation = AggregationBuilder(
        cluster_window=options.cluster_window,
        cluster_anchor=options.cluster_anchor,
        schedule_bounds=schedule.bounds,
        display_range=display_range,
        outside_limit=options.limit if options.list_outside else 0,
        retain_git_identities=options.marker_style is MarkerStyle.IDENTITY,
        hide_days=options.hide_days,
        hide_empty_days=options.hide_empty_days,
    )
    observation_scope = ObservationScope(selected_range, options.git_identities)
    classifier = ActivityClassifier(
        timezone_value=timezone_value,
        schedule=schedule,
        marker_consumer=aggregation.add,
    )
    try:
        collection = collect(
            options,
            observation_consumer=classifier.consume,
            observation_scope=observation_scope,
            git_collector=git_collector,
            repository_resolver=repository_resolver,
            file_change_collector=file_change_collector,
            tag_collector=tag_collector,
            reflog_collector=reflog_collector,
            filesystem_collector=filesystem_collector,
        )
        if not collection.any_collector_succeeded:
            return ExecutionResult(collection, None, None)

        chart = aggregation.build()
        ledger = build_coverage(
            collection,
            options,
            observations=classifier.observation_counts,
            plotting=classifier.plotting_counts,
        )
        if ledger.markers_plotted != chart.event_count:
            raise RuntimeError("coverage marker totals do not match the classified marker stream")
        report = build_report(
            chart,
            build_report_context(
                collection,
                options,
                ledger,
                range_label=range_label,
                timezone_label=timezone_value.key,
                schedule_label=str(schedule),
            ),
        )
        return ExecutionResult(collection, report, ledger)
    finally:
        aggregation.close()


__all__ = ["ExecutionResult", "execute"]
