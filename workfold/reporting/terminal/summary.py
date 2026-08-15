"""Compact summary and verbose scope details."""

from __future__ import annotations

from workfold.application.report import Report
from workfold.configuration.options import BandLabel
from workfold.domain.observations import Source
from workfold.folding.bands import ClusterAnchor
from workfold.reporting.sanitization import sanitize_terminal_text
from workfold.reporting.terminal.coverage_details import coverage_details
from workfold.reporting.terminal.coverage_scope import enabled_sources
from workfold.reporting.terminal.coverage_status import COMPLETE_COVERAGE_STATUS, coverage_status_label
from workfold.reporting.terminal.layout import aligned_fact_lines, fact_lines, fit_plain, format_duration
from workfold.reporting.terminal.options import TerminalOptions
from workfold.reporting.terminal.scope_details import extent_label, identity_label, ignore_label, source_label


def render_summary(report: Report, options: TerminalOptions) -> str:
    width = options.width
    facts = list(_summary_stat_facts(report))
    hidden_parts = _hidden_event_parts(report)
    if hidden_parts:
        facts.append(("Hidden", " · ".join(hidden_parts)))
    context = report.context
    status = coverage_status_label(context.collection, context.coverage, context.scope)
    coverage_status = _default_coverage_status(status)
    if coverage_status is not None:
        facts.append(("Coverage", coverage_status))
    lines = aligned_fact_lines(facts, width)
    if options.coverage or options.verbose:
        lines.append("Coverage details:")
        for detail in coverage_details(context.coverage, context.collection, context.scope):
            lines.extend(fact_lines("  -", detail, width))
    return "\n".join(fit_plain(line, width) for line in lines)


def render_details(
    report: Report,
    width: int,
    *,
    band_label: BandLabel = BandLabel.RANGE,
    show_empty_bands: bool = False,
) -> str:
    context = report.context
    scope = context.scope
    aggregation = report.aggregation
    sources = _enabled_sources(enabled_sources(scope), aggregation.source_counts)
    status = coverage_status_label(context.collection, context.coverage, scope)
    lines = ["Details"]
    lines.extend(fact_lines("Scope", _scope_label(sources, scope.profile_name), width))
    lines.extend(fact_lines("Period", f"{scope.period_label} · {scope.timezone_name}", width))
    lines.extend(fact_lines("Schedule", str(scope.schedule), width))
    lines.extend(fact_lines("Coverage", status, width))

    hidden_parts = _hidden_event_parts(report)
    if hidden_parts:
        lines.extend(fact_lines("Hidden events", " · ".join(hidden_parts), width))

    anchor_label = {
        ClusterAnchor.EVENT: "event",
        ClusterAnchor.MIDNIGHT: "midnight",
    }[aggregation.cluster_anchor]
    lines.extend(fact_lines("Cluster anchor", anchor_label, width))
    lines.extend(fact_lines("Cluster window", format_duration(aggregation.cluster_window), width))
    lines.extend(fact_lines("Band labels", band_label.value, width))
    compression = (
        "disabled; all fixed bands in the display range shown; busy cells use exact symbol×count"
        if show_empty_bands
        else "empty time omitted; gaps of at least one cluster window marked; busy cells use exact symbol×count"
    )
    lines.extend(fact_lines("Compression", compression, width))
    lines.extend(fact_lines("Collector selectors", source_label(scope), width))
    identities = identity_label(scope)
    if identities is not None:
        lines.extend(fact_lines("Git identities", identities, width))
    extents = extent_label(context.collection, scope)
    if extents is not None:
        lines.extend(fact_lines("Extents", extents, width))
    ignore_policy = ignore_label(scope, context.collection)
    if ignore_policy is not None:
        lines.extend(fact_lines("Filesystem policy", ignore_policy, width))
    if scope.exclusions:
        lines.extend(fact_lines("Explicit exclusions", ", ".join(scope.exclusions), width))
    return "\n".join(fit_plain(line, width) for line in lines)


def _summary_stat_facts(report: Report) -> tuple[tuple[str, str], ...]:
    aggregation = report.aggregation
    inside_percentage = _percentage(aggregation.within_schedule_count, aggregation.event_count)
    outside_percentage = _percentage(aggregation.outside_schedule_count, aggregation.event_count)
    weekday_count = aggregation.event_count - aggregation.weekend_count
    weekday_percentage = _percentage(weekday_count, aggregation.event_count)
    weekend_percentage = _percentage(aggregation.weekend_count, aggregation.event_count)
    return (
        ("Events", f"{aggregation.event_count:,}"),
        (
            "Schedule",
            f"{aggregation.within_schedule_count:,} inside ({inside_percentage}) · "
            f"{aggregation.outside_schedule_count:,} outside ({outside_percentage})",
        ),
        (
            "Calendar",
            f"{weekday_count:,} weekday ({weekday_percentage}) · "
            f"{aggregation.weekend_count:,} weekend ({weekend_percentage})",
        ),
    )


def _hidden_event_parts(report: Report) -> list[str]:
    aggregation = report.aggregation
    parts: list[str] = []
    if aggregation.hidden_before.total:
        parts.append(f"before display {aggregation.hidden_before.total:,}")
    if aggregation.hidden_after.total:
        parts.append(f"after display {aggregation.hidden_after.total:,}")
    if aggregation.hidden_weekday_counts:
        total = aggregation.hidden_weekday_event_count
        day_names = ", ".join(
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[int(day)] for day, _ in aggregation.hidden_weekday_counts
        )
        event_word = "event" if total == 1 else "events"
        column_word = "column" if len(aggregation.hidden_weekday_counts) == 1 else "columns"
        parts.append(f"{total:,} {event_word} in {day_names} {column_word}")
    return parts


def _scope_label(sources: tuple[Source, ...], profile: str) -> str:
    source_names = {Source.GIT: "Git", Source.FILESYSTEM: "filesystem"}
    source_text = " + ".join(source_names[source] for source in sources) or "none"
    safe_profile = sanitize_terminal_text(profile)
    return f"{source_text} · {safe_profile}" if safe_profile else source_text


def _percentage(count: int, total: int) -> str:
    return "n/a" if total == 0 else f"{count / total * 100:.1f}%"


def _default_coverage_status(status: str) -> str | None:
    if status == COMPLETE_COVERAGE_STATUS:
        return None
    qualified_prefix = f"{COMPLETE_COVERAGE_STATUS}; "
    if status.startswith(qualified_prefix):
        return status.removeprefix(qualified_prefix)
    return status


def _enabled_sources(enabled: tuple[Source, ...], counts: tuple[tuple[Source, int], ...]) -> tuple[Source, ...]:
    values = enabled or tuple(key for key, _count in counts)
    order = {Source.GIT: 0, Source.FILESYSTEM: 1}
    return tuple(sorted(set(values), key=order.__getitem__))
