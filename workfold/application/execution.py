"""Renderer-neutral execution of one Workfold collection and report build."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from workfold.application.collection import Collection, CollectorServices, collect
from workfold.application.coverage import build_coverage
from workfold.application.report import (
    Report,
    ReportContext,
    ReportRequirements,
    build_report,
    matches_event_list,
)
from workfold.application.resolution import resolve_date_range, resolve_schedule, resolve_timezone_selection
from workfold.collection.diagnostics import diagnostics_are_partial
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import CoverageLedger
from workfold.domain.observations import ClassifiedMarker
from workfold.domain.scope import ObservationScope
from workfold.folding import AggregationBuilder
from workfold.folding.pipeline import ActivityClassifier


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Domain result available to terminal and future report renderers."""

    collection: Collection
    report: Report | None
    coverage: CoverageLedger | None

    @property
    def is_partial(self) -> bool:
        return diagnostics_are_partial(self.collection.diagnostics) or bool(
            self.coverage and self.coverage.has_operational_errors
        )


def execute(
    options: RunOptions,
    collectors: CollectorServices,
    report_requirements: ReportRequirements,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExecutionResult:
    """Collect, classify, aggregate, and build renderer-neutral report data."""

    environment = os.environ if environ is None else environ
    clock_value = datetime.now(timezone.utc) if now is None else now
    timezone_value = resolve_timezone_selection(options, environment)
    time_selection = resolve_date_range(options, timezone_value, clock_value)
    schedule = resolve_schedule(options)
    display_range = (
        (options.display_hours.start_minute, options.display_hours.end_minute)
        if options.display_hours is not None
        else None
    )
    event_list = report_requirements.event_list

    def requested_for_list(marker: ClassifiedMarker) -> bool:
        assert event_list is not None
        return matches_event_list(marker, event_list)

    aggregation = AggregationBuilder(
        cluster_window=options.cluster_window,
        cluster_anchor=options.cluster_anchor,
        schedule_bounds=schedule.bounds,
        display_range=display_range,
        listed_marker_limit=report_requirements.event_limit,
        listed_marker_predicate=None if event_list is None else requested_for_list,
        retain_git_identities=report_requirements.retain_git_identities,
        hide_days=options.hide_days,
        hide_empty_days=options.hide_empty_days,
    )
    observation_scope = ObservationScope(time_selection.ranges, options.git_identities)
    classifier = ActivityClassifier(
        timezone_value=timezone_value,
        schedule=schedule,
        marker_consumer=aggregation.add,
    )
    try:
        collection = collect(
            options,
            collectors,
            observation_consumer=classifier.consume,
            observation_scope=observation_scope,
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
            ReportContext(
                options=options,
                collection=collection,
                time_selection=time_selection,
                timezone=timezone_value,
                schedule=schedule,
                coverage=ledger,
            ),
            report_requirements.event_list,
        )
        return ExecutionResult(collection, report, ledger)
    finally:
        aggregation.close()


__all__ = ["ExecutionResult", "execute"]
