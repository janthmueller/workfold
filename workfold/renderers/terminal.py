"""Deterministic terminal renderer for Workfold reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from io import StringIO

from rich.console import Console
from rich.text import Text

from workfold.aggregation import NANOSECONDS_PER_MINUTE, NANOSECONDS_PER_SECOND, ClusterCell, TimeCluster
from workfold.models import ClassifiedMarker, Source, Weekday
from workfold.reports import COMPLETE_COVERAGE_STATUS, OutsideEvent, Report
from workfold.sanitization import display_width, pad_right, sanitize_terminal_text, truncate_end, truncate_middle

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_GAP_THRESHOLD_NS = 60 * NANOSECONDS_PER_MINUTE
_MAX_LITERAL_EVENT_LINES = 3
_EVENT_VISUALS: dict[tuple[Source, bool], tuple[str, str]] = {
    (Source.GIT, True): ("●", "green"),
    (Source.FILESYSTEM, True): ("■", "bright_blue"),
    (Source.GIT, False): ("○", "bold bright_red"),
    (Source.FILESYSTEM, False): ("□", "bold bright_red"),
}


@dataclass(frozen=True, slots=True)
class TerminalOptions:
    """Presentation-only options resolved by the CLI environment adapter."""

    width: int = 80
    color: bool = False
    list_outside: bool = False
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.width < 60:
            raise ValueError("terminal width must be at least 60 columns")


def terminal_color_enabled(
    *,
    no_color: bool,
    environ: Mapping[str, str],
    stdout_is_tty: bool,
) -> bool:
    """Resolve the MVP color policy from flags and process capabilities."""

    return not no_color and "NO_COLOR" not in environ and environ.get("TERM", "").casefold() != "dumb" and stdout_is_tty


def render_terminal(report: Report, *, options: TerminalOptions | None = None) -> str:
    """Render *report* as terminal text ending in exactly one newline."""

    resolved = options or TerminalOptions()
    sections: list[tuple[Text, ...]] = [
        _render_chart(report, resolved),
        _render_legend(report, resolved.width),
        _plain_section(_render_summary(report, resolved.width)),
    ]
    if resolved.verbose:
        sections.append(_plain_section(_render_details(report, resolved.width), heading=True))
    if resolved.list_outside:
        sections.append(_plain_section(_render_outside(report, resolved.width), heading=True))

    stream = StringIO()
    console = Console(
        file=stream,
        width=resolved.width,
        color_system="standard" if resolved.color else None,
        force_terminal=resolved.color,
        no_color=not resolved.color,
        highlight=False,
        legacy_windows=False,
    )
    populated = tuple(section for section in sections if section)
    for section_index, section in enumerate(populated):
        if section_index:
            console.print()
        for line in section:
            console.print(line, soft_wrap=False)
    return stream.getvalue().rstrip("\n") + "\n"


def _render_chart(report: Report, options: TerminalOptions) -> tuple[Text, ...]:
    aggregation = report.aggregation
    labels, time_width, day_width = _chart_layout(report, options.width)

    header = Text(pad_right("Time band", time_width), style="bold")
    for weekday, width in zip(_WEEKDAYS, (day_width,) * len(_WEEKDAYS), strict=True):
        header.append(" ")
        header.append(_center(weekday, width), style="bold")

    if not aggregation.clusters:
        message = (
            "No events in the displayed time range." if aggregation.event_count else "No events in selected scope."
        )
        return (header, Text(message, style="dim"))

    lines = [header]
    previous: TimeCluster | None = None
    for cluster, label in zip(aggregation.clusters, labels, strict=True):
        if previous is not None:
            gap_ns = cluster.start_time_ns - previous.end_time_ns
            if gap_ns >= _GAP_THRESHOLD_NS:
                lines.append(Text(pad_right(f"⋮ {_format_ns_duration(gap_ns)}", time_width), style="dim"))
        lines.extend(_cluster_lines(cluster, label=label, time_width=time_width, day_width=day_width))
        previous = cluster
    return tuple(lines)


def _render_legend(report: Report, width: int) -> tuple[Text, ...]:
    inside_sources: set[Source] = set()
    outside_sources: set[Source] = set()
    for cluster in report.aggregation.clusters:
        for cell in cluster.cells:
            for marker in cell.markers:
                target = inside_sources if marker.within_schedule else outside_sources
                target.add(_marker_source(marker))

    items: list[Text] = []
    for source in (Source.GIT, Source.FILESYSTEM):
        if source in inside_sources:
            items.append(_source_legend_item(source, within_schedule=True))

    represented_outside = outside_sources & inside_sources
    if represented_outside:
        items.append(_outside_legend_item(represented_outside))
    for source in (Source.GIT, Source.FILESYSTEM):
        if source in outside_sources - inside_sources:
            items.append(_outside_only_legend_item(source))

    _labels, _time_width, day_width = _chart_layout(report, width)
    if any(
        cell.event_count > day_width * _MAX_LITERAL_EVENT_LINES
        for cluster in report.aggregation.clusters
        for cell in cluster.cells
    ):
        items.append(Text("×N exact count", style="dim"))

    lines = list(_pack_legend_items(items, width))
    schedule = f"Working hours: {sanitize_terminal_text(report.context.schedule_label)}"
    lines.extend(Text(chunk) for chunk in _column_chunks(schedule, width))
    return tuple(lines)


def _chart_layout(report: Report, width: int) -> tuple[tuple[str, ...], int, int]:
    labels = tuple(_cluster_label(cluster) for cluster in report.aggregation.clusters)
    time_width = max((display_width("Time band"), *(display_width(label) for label in labels)))
    day_width = max(3, (width - time_width - len(_WEEKDAYS)) // len(_WEEKDAYS))
    return labels, time_width, day_width


def _source_legend_item(source: Source, *, within_schedule: bool) -> Text:
    symbol, style = _EVENT_VISUALS[(source, within_schedule)]
    label = "Git" if source is Source.GIT else "Filesystem"
    return Text(f"{symbol} {label}", style=style)


def _outside_legend_item(sources: set[Source]) -> Text:
    item = Text()
    for index, source in enumerate((Source.GIT, Source.FILESYSTEM)):
        if source not in sources:
            continue
        if index and item:
            item.append("/")
        symbol, style = _EVENT_VISUALS[(source, False)]
        item.append(symbol, style=style)
    item.append(" Outside working hours", style="bold bright_red")
    return item


def _outside_only_legend_item(source: Source) -> Text:
    symbol, style = _EVENT_VISUALS[(source, False)]
    label = "Git" if source is Source.GIT else "Filesystem"
    return Text(f"{symbol} {label} outside working hours", style=style)


def _pack_legend_items(items: list[Text], width: int) -> tuple[Text, ...]:
    if not items:
        return ()
    separator = Text(" · ", style="dim")
    lines: list[Text] = []
    current = Text()
    for item in items:
        required = item.cell_len + (separator.cell_len if current else 0)
        if current and current.cell_len + required > width:
            lines.append(current)
            current = Text()
        if current:
            current.append_text(separator.copy())
        current.append_text(item.copy())
    if current:
        lines.append(current)
    return tuple(lines)


def _cluster_lines(
    cluster: TimeCluster,
    *,
    label: str,
    time_width: int,
    day_width: int,
) -> tuple[Text, ...]:
    cells = tuple(_cell_lines(cluster.cell(weekday), day_width) for weekday in Weekday)
    height = max((len(lines) for lines in cells), default=1)
    rendered: list[Text] = []
    for line_index in range(height):
        time_label = label if line_index == 0 else "↳"
        line = (
            Text(pad_right(time_label, time_width))
            if line_index == 0
            else Text(pad_right(time_label, time_width), style="dim")
        )
        for cell_lines in cells:
            line.append(" ")
            content = cell_lines[line_index] if line_index < len(cell_lines) else Text()
            line.append_text(content)
            line.append(" " * max(0, day_width - content.cell_len))
        rendered.append(line)
    return tuple(rendered)


def _cell_lines(cell: ClusterCell | None, width: int) -> tuple[Text, ...]:
    if cell is None:
        return (Text(),)
    if cell.event_count <= width * _MAX_LITERAL_EVENT_LINES:
        return tuple(_marker_run(cell.markers[index : index + width]) for index in range(0, cell.event_count, width))
    return _compact_cell_lines(cell.markers, width)


def _marker_run(markers: tuple[ClassifiedMarker, ...]) -> Text:
    line = Text()
    for marker in markers:
        symbol, style = _event_visual(marker)
        line.append(symbol, style=style)
    return line


def _compact_cell_lines(markers: tuple[ClassifiedMarker, ...], width: int) -> tuple[Text, ...]:
    counts = Counter((_marker_source(marker), marker.within_schedule) for marker in markers)
    ordered_keys = (
        (Source.GIT, True),
        (Source.FILESYSTEM, True),
        (Source.GIT, False),
        (Source.FILESYSTEM, False),
    )
    lines: list[Text] = []
    current = Text()
    for key in ordered_keys:
        count = counts[key]
        if not count:
            continue
        symbol, style = _EVENT_VISUALS[key]
        token = Text(f"{symbol}×{count:,}", style=style)
        if token.cell_len > width:
            if current:
                lines.append(current)
                current = Text()
            lines.append(Text(f"{symbol}×", style=style))
            digits = str(count)
            lines.extend(Text(digits[index : index + width], style=style) for index in range(0, len(digits), width))
            continue
        separator_width = 1 if current else 0
        if current and current.cell_len + separator_width + token.cell_len > width:
            lines.append(current)
            current = Text()
            separator_width = 0
        if separator_width:
            current.append(" ")
        current.append_text(token)
    if current:
        lines.append(current)
    return tuple(lines)


def _event_visual(marker: ClassifiedMarker) -> tuple[str, str]:
    return _EVENT_VISUALS[(_marker_source(marker), marker.within_schedule)]


def _marker_source(marker: ClassifiedMarker) -> Source:
    return marker.marker.origin.source


def _cluster_label(cluster: TimeCluster) -> str:
    start = _format_time_ns(cluster.start_time_ns)
    end = _format_time_ns(cluster.end_time_ns)
    return start if start == end else f"{start}–{end}"


def _format_time_ns(time_ns: int) -> str:
    total_seconds = time_ns // NANOSECONDS_PER_SECOND
    hour, remainder = divmod(total_seconds, 3_600)
    minute = remainder // 60
    return f"{hour:02d}:{minute:02d}"


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
            seconds_text = f"{seconds}.{fraction}"
            parts.append(f"{seconds_text}s")
        else:
            parts.append(f"{seconds}s")
    return "".join(parts)


def _format_ns_duration(value_ns: int) -> str:
    total_seconds = value_ns // NANOSECONDS_PER_SECOND
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts and seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


def _center(value: str, width: int) -> str:
    fitted = truncate_end(value, width)
    padding = max(0, width - display_width(fitted))
    left = padding // 2
    return " " * left + fitted + " " * (padding - left)


def _plain_section(value: str, *, heading: bool = False) -> tuple[Text, ...]:
    lines = value.splitlines()
    return tuple(Text(line, style="bold") if heading and index == 0 else Text(line) for index, line in enumerate(lines))


def _render_summary(report: Report, width: int) -> str:
    facts = list(_summary_stat_facts(report))
    hidden_parts: list[str] = []
    if report.aggregation.hidden_before.total:
        hidden_parts.append(f"before display {report.aggregation.hidden_before.total:,}")
    if report.aggregation.hidden_after.total:
        hidden_parts.append(f"after display {report.aggregation.hidden_after.total:,}")
    if hidden_parts:
        facts.append(("Hidden", " · ".join(hidden_parts)))
    coverage_status = _default_coverage_status(report.context.coverage_status)
    if coverage_status is not None:
        facts.append(("Coverage", coverage_status))
    lines = _aligned_fact_lines(facts, width)
    if report.context.coverage_details:
        lines.append("Coverage details:")
        for detail in report.context.coverage_details:
            lines.extend(_fact_lines("  -", detail, width))
    return "\n".join(_fit_plain(line, width) for line in lines)


def _render_details(report: Report, width: int) -> str:
    """Render operational and exact collector configuration for verbose output."""

    context = report.context
    aggregation = report.aggregation
    enabled_sources = _enabled_sources(context.enabled_sources, aggregation.source_counts)
    lines = ["Details"]
    lines.extend(_fact_lines("Scope", _scope_label(enabled_sources, context.profile_label), width))
    lines.extend(_fact_lines("Period", f"{context.range_label} · {context.timezone_label}", width))
    lines.extend(_fact_lines("Schedule", context.schedule_label, width))
    lines.extend(_fact_lines("Coverage", context.coverage_status, width))

    hidden_parts: list[str] = []
    if aggregation.hidden_before.total:
        hidden_parts.append(f"before display {aggregation.hidden_before.total:,}")
    if aggregation.hidden_after.total:
        hidden_parts.append(f"after display {aggregation.hidden_after.total:,}")
    if hidden_parts:
        lines.extend(_fact_lines("Hidden events", " · ".join(hidden_parts), width))

    lines.extend(_fact_lines("Cluster window", _format_duration(aggregation.cluster_window), width))
    lines.extend(_fact_lines("Compression", "empty time omitted; busy cells use exact symbol×count", width))
    lines.extend(_fact_lines("Collector selectors", context.source_label, width))
    if context.identity_label is not None:
        lines.extend(_fact_lines("Git identities", context.identity_label, width))
    if context.extent_label is not None:
        lines.extend(_fact_lines("Extents", context.extent_label, width))
    if context.ignore_label is not None:
        lines.extend(_fact_lines("Filesystem policy", context.ignore_label, width))
    if context.exclusions:
        lines.extend(_fact_lines("Explicit exclusions", ", ".join(context.exclusions), width))
    return "\n".join(_fit_plain(line, width) for line in lines)


def _scope_label(sources: tuple[Source, ...], profile: str) -> str:
    source_names = {
        Source.GIT: "Git",
        Source.FILESYSTEM: "filesystem",
    }
    source_text = " + ".join(source_names[source] for source in sources) or "none"
    safe_profile = sanitize_terminal_text(profile)
    return f"{source_text} · {safe_profile}" if safe_profile else source_text


def _render_outside(report: Report, width: int) -> str:
    aggregation = report.aggregation
    if not report.outside_events:
        return "Outside-hours events\nNone"

    shown = len(report.outside_events)
    total = aggregation.outside_marker_count
    heading = f"Outside-hours events (showing {shown} of {total}"
    if aggregation.outside_omitted_count:
        heading += f"; {aggregation.outside_omitted_count} older omitted"
    heading += ")"

    timestamp_width = 38
    role_width = 18
    identity_width = 12
    fixed_width = timestamp_width + role_width + identity_width + 3
    detail_width = max(1, width - fixed_width)
    lines = [
        _fit_plain(heading, width),
        (
            f"{pad_right('Timestamp', timestamp_width)} "
            f"{pad_right('Source/role', role_width)} "
            f"{pad_right('Identity', identity_width)} "
            f"{truncate_end('Repository/root and detail', detail_width)}"
        ),
    ]
    for event in report.outside_events:
        lines.extend(_outside_lines(event, detail_width, width))
    return "\n".join(_fit_plain(line, width) for line in lines)


def _outside_lines(event: OutsideEvent, detail_width: int, width: int) -> tuple[str, ...]:
    timestamp = _format_exact_local_timestamp(event)
    source_role = f"{_source_short_label(event.source)}/{_roles_label(event.timestamp_roles)}"
    identity = (
        event.commit_id[:10]
        if event.commit_id is not None
        else (event.ref_name if event.ref_name is not None else event.provenance_id[:10])
    )
    detail_parts = [str(event.repository_or_root)]
    if event.commit_id is not None and event.ref_name is not None:
        detail_parts.append(f"[{event.ref_name}]")
    detail = " | ".join(sanitize_terminal_text(part) for part in detail_parts)
    primary = (
        f"{pad_right(sanitize_terminal_text(timestamp), 38)} "
        f"{pad_right(sanitize_terminal_text(source_role), 18)} "
        f"{pad_right(sanitize_terminal_text(identity), 12)} "
        f"{truncate_middle(detail, detail_width)}"
    )
    if not event.description:
        return (primary,)

    description_prefix = "  Detail: "
    description = truncate_end(
        sanitize_terminal_text(event.description),
        width - display_width(description_prefix),
    )
    return (primary, description_prefix + description)


def _format_exact_local_timestamp(event: OutsideEvent) -> str:
    """Render the selected-zone instant without discarding nanoseconds."""

    local = event.local_datetime
    offset = local.utcoffset()
    if offset is None:
        raise ValueError("outside-event timestamps must be timezone-aware")
    total_offset_seconds = int(offset.total_seconds())
    sign = "+" if total_offset_seconds >= 0 else "-"
    absolute_offset = abs(total_offset_seconds)
    hours, remainder = divmod(absolute_offset, 3_600)
    minutes, seconds = divmod(remainder, 60)
    offset_text = f"{sign}{hours:02d}:{minutes:02d}"
    if seconds:
        offset_text += f":{seconds:02d}"

    nanoseconds = event.occurred_at_utc_ns % 1_000_000_000
    fraction = f".{nanoseconds:09d}" if nanoseconds else ""
    return local.strftime("%Y-%m-%dT%H:%M:%S") + fraction + offset_text


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


def _percentage(count: int, total: int) -> str:
    return "n/a" if total == 0 else f"{count / total * 100:.1f}%"


def _default_coverage_status(status: str) -> str | None:
    """Hide ordinary success and shorten qualified success in the default view."""

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


def _source_short_label(source: Source) -> str:
    return "git" if source is Source.GIT else "fs"


def _roles_label(roles: tuple[str, ...]) -> str:
    return "+".join(role.removeprefix("git_").removeprefix("fs_") for role in roles)


def _aligned_fact_lines(facts: list[tuple[str, str]], width: int) -> list[str]:
    if not facts:
        return []
    safe_facts = [(sanitize_terminal_text(label), sanitize_terminal_text(value)) for label, value in facts]
    label_width = max(display_width(label) for label, _value in safe_facts)
    lines: list[str] = []
    for label, value in safe_facts:
        prefix = f"{pad_right(label, label_width)}  "
        available = max(1, width - display_width(prefix))
        chunks = _column_chunks(value, available)
        if not chunks:
            lines.append(prefix.rstrip())
            continue
        indent = " " * display_width(prefix)
        lines.extend((prefix + chunks[0], *(indent + chunk for chunk in chunks[1:])))
    return lines


def _fact_lines(label: str, value: object, width: int) -> list[str]:
    safe_label = sanitize_terminal_text(label)
    safe_value = sanitize_terminal_text(value)
    prefix = f"{safe_label}: "
    available = max(1, width - display_width(prefix))
    chunks = _column_chunks(safe_value, available)
    if not chunks:
        return [prefix]
    indent = " " * display_width(prefix)
    return [prefix + chunks[0], *(indent + chunk for chunk in chunks[1:])]


def _column_chunks(text: str, width: int) -> list[str]:
    if not text:
        return []
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current = ""
    for word in words:
        if display_width(word) > width:
            if current:
                chunks.append(current)
                current = ""
            hard_chunks = _hard_column_chunks(word, width)
            chunks.extend(hard_chunks[:-1])
            current = hard_chunks[-1]
            continue

        candidate = word if not current else f"{current} {word}"
        if display_width(candidate) <= width:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _hard_column_chunks(text: str, width: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0
    for character in text:
        character_width = display_width(character)
        if current and current_width + character_width > width:
            chunks.append("".join(current))
            current = []
            current_width = 0
        current.append(character)
        current_width += character_width
    if current:
        chunks.append("".join(current))
    return chunks


def _fit_plain(line: str, width: int) -> str:
    return truncate_end(line, width)
