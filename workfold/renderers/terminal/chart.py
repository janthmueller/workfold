"""Sparse weekly terminal chart and conditional legend."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from rich.text import Text

from workfold.aggregation import NANOSECONDS_PER_MINUTE, NANOSECONDS_PER_SECOND, ClusterCell, MarkerRun, TimeCluster
from workfold.models import Source, Weekday
from workfold.renderers.terminal.options import TerminalOptions
from workfold.renderers.terminal.text import center, column_chunks
from workfold.reports import Report
from workfold.sanitization import display_width, pad_right, sanitize_terminal_text

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
GAP_THRESHOLD_NS = 60 * NANOSECONDS_PER_MINUTE
MAX_LITERAL_EVENT_LINES = 3
EVENT_VISUALS: dict[tuple[Source, bool], tuple[str, str]] = {
    (Source.GIT, True): ("●", "green"),
    (Source.FILESYSTEM, True): ("■", "bright_blue"),
    (Source.GIT, False): ("○", "bold bright_red"),
    (Source.FILESYSTEM, False): ("□", "bold bright_red"),
}


def render_chart(report: Report, options: TerminalOptions) -> Iterable[Text]:
    aggregation = report.aggregation
    time_width, day_width = chart_layout(report, options.width)

    header = Text(pad_right("Time band", time_width), style="bold")
    for weekday, width in zip(WEEKDAYS, (day_width,) * len(WEEKDAYS), strict=True):
        header.append(" ")
        header.append(center(weekday, width), style="bold")
    yield header

    if not aggregation.clusters:
        message = (
            "No events in the displayed time range." if aggregation.event_count else "No events in selected scope."
        )
        yield Text(message, style="dim")
        return

    previous: TimeCluster | None = None
    for cluster in aggregation.clusters:
        if previous is not None:
            gap_ns = cluster.start_time_ns - previous.end_time_ns
            if gap_ns >= GAP_THRESHOLD_NS:
                yield Text(pad_right(f"⋮ {_format_ns_duration(gap_ns)}", time_width), style="dim")
        yield from _cluster_lines(
            cluster,
            label=_cluster_label(cluster),
            time_width=time_width,
            day_width=day_width,
        )
        previous = cluster


def render_legend(report: Report, width: int) -> tuple[Text, ...]:
    aggregation = report.aggregation
    inside_sources = {
        source for source in (Source.GIT, Source.FILESYSTEM) if aggregation.count_for_visual(source, True)
    }
    outside_sources = {
        source for source in (Source.GIT, Source.FILESYSTEM) if aggregation.count_for_visual(source, False)
    }

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

    _time_width, day_width = chart_layout(report, width)
    if aggregation.max_cell_event_count > day_width * MAX_LITERAL_EVENT_LINES:
        items.append(Text("×N exact count", style="dim"))

    lines = list(_pack_legend_items(items, width))
    schedule = f"Working hours: {sanitize_terminal_text(report.context.schedule_label)}"
    lines.extend(Text(chunk) for chunk in column_chunks(schedule, width))
    return tuple(lines)


def chart_layout(report: Report, width: int) -> tuple[int, int]:
    label_width = 11 if report.aggregation.has_multi_minute_cluster else 5
    time_width = max(label_width, display_width("Time band"))
    day_width = max(3, (width - time_width - len(WEEKDAYS)) // len(WEEKDAYS))
    return time_width, day_width


def _source_legend_item(source: Source, *, within_schedule: bool) -> Text:
    symbol, style = EVENT_VISUALS[(source, within_schedule)]
    label = "Git" if source is Source.GIT else "Filesystem"
    return Text(f"{symbol} {label}", style=style)


def _outside_legend_item(sources: set[Source]) -> Text:
    item = Text()
    for index, source in enumerate((Source.GIT, Source.FILESYSTEM)):
        if source not in sources:
            continue
        if index and item:
            item.append("/")
        symbol, style = EVENT_VISUALS[(source, False)]
        item.append(symbol, style=style)
    item.append(" Outside working hours", style="bold bright_red")
    return item


def _outside_only_legend_item(source: Source) -> Text:
    symbol, style = EVENT_VISUALS[(source, False)]
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


def _cluster_lines(cluster: TimeCluster, *, label: str, time_width: int, day_width: int) -> tuple[Text, ...]:
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
    if not cell.compacted and cell.event_count <= width * MAX_LITERAL_EVENT_LINES:
        visuals = tuple((run.source, run.within_schedule) for run in cell.runs for _event_index in range(run.count))
        return tuple(_marker_run(visuals[index : index + width]) for index in range(0, cell.event_count, width))
    return _compact_cell_lines(cell.runs, width)


def _marker_run(visuals: tuple[tuple[Source, bool], ...]) -> Text:
    line = Text()
    for visual in visuals:
        symbol, style = EVENT_VISUALS[visual]
        line.append(symbol, style=style)
    return line


def _compact_cell_lines(runs: tuple[MarkerRun, ...], width: int) -> tuple[Text, ...]:
    counts: Counter[tuple[Source, bool]] = Counter()
    for run in runs:
        counts[(run.source, run.within_schedule)] += run.count
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
        symbol, style = EVENT_VISUALS[key]
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


def _cluster_label(cluster: TimeCluster) -> str:
    start = _format_time_ns(cluster.start_time_ns)
    end = _format_time_ns(cluster.end_time_ns)
    return start if start == end else f"{start}–{end}"


def _format_time_ns(time_ns: int) -> str:
    total_seconds = time_ns // NANOSECONDS_PER_SECOND
    hour, remainder = divmod(total_seconds, 3_600)
    minute = remainder // 60
    return f"{hour:02d}:{minute:02d}"


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
