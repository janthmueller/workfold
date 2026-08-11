"""Compact summary and verbose scope details."""

from __future__ import annotations

from datetime import timedelta

from workfold.models import Source
from workfold.renderers.terminal.text import aligned_fact_lines, fact_lines, fit_plain
from workfold.reports import COMPLETE_COVERAGE_STATUS, Report
from workfold.sanitization import sanitize_terminal_text


def render_summary(report: Report, width: int) -> str:
    facts = list(_summary_stat_facts(report))
    hidden_parts = _hidden_event_parts(report)
    if hidden_parts:
        facts.append(("Hidden", " · ".join(hidden_parts)))
    coverage_status = _default_coverage_status(report.context.coverage_status)
    if coverage_status is not None:
        facts.append(("Coverage", coverage_status))
    lines = aligned_fact_lines(facts, width)
    if report.context.coverage_details:
        lines.append("Coverage details:")
        for detail in report.context.coverage_details:
            lines.extend(fact_lines("  -", detail, width))
    return "\n".join(fit_plain(line, width) for line in lines)


def render_details(report: Report, width: int) -> str:
    context = report.context
    aggregation = report.aggregation
    enabled_sources = _enabled_sources(context.enabled_sources, aggregation.source_counts)
    lines = ["Details"]
    lines.extend(fact_lines("Scope", _scope_label(enabled_sources, context.profile_label), width))
    lines.extend(fact_lines("Period", f"{context.range_label} · {context.timezone_label}", width))
    lines.extend(fact_lines("Schedule", context.schedule_label, width))
    lines.extend(fact_lines("Coverage", context.coverage_status, width))

    hidden_parts = _hidden_event_parts(report)
    if hidden_parts:
        lines.extend(fact_lines("Hidden events", " · ".join(hidden_parts), width))

    lines.extend(fact_lines("Cluster window", _format_duration(aggregation.cluster_window), width))
    lines.extend(fact_lines("Compression", "empty time omitted; busy cells use exact symbol×count", width))
    lines.extend(fact_lines("Collector selectors", context.source_label, width))
    if context.identity_label is not None:
        lines.extend(fact_lines("Git identities", context.identity_label, width))
    if context.extent_label is not None:
        lines.extend(fact_lines("Extents", context.extent_label, width))
    if context.ignore_label is not None:
        lines.extend(fact_lines("Filesystem policy", context.ignore_label, width))
    if context.exclusions:
        lines.extend(fact_lines("Explicit exclusions", ", ".join(context.exclusions), width))
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


def _format_duration(value: timedelta) -> str:
    total_microseconds = value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    total_seconds, microseconds = divmod(total_microseconds, 1_000_000)
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or microseconds or not parts:
        if microseconds:
            fraction = f"{microseconds:06d}".rstrip("0")
            parts.append(f"{seconds}.{fraction}s")
        else:
            parts.append(f"{seconds}s")
    return "".join(parts)


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
