"""Sparse weekly terminal chart and conditional legend."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from rich.text import Text

from workfold.aggregation import NANOSECONDS_PER_MINUTE, NANOSECONDS_PER_SECOND, ClusterCell, MarkerRun, TimeCluster
from workfold.config import MarkerStyle
from workfold.models import Source, Weekday
from workfold.renderers.terminal.identity import (
    IdentitySymbol,
    allocate_identity_symbols,
    marker_identity_label,
)
from workfold.renderers.terminal.options import TerminalOptions
from workfold.renderers.terminal.text import center, column_chunks, rich_text_chunks
from workfold.reports import Report
from workfold.sanitization import display_width, pad_right, sanitize_terminal_text

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
GAP_THRESHOLD_NS = 60 * NANOSECONDS_PER_MINUTE
MAX_LITERAL_EVENT_LINES = 3
GRID_STYLE = "dim"
EVENT_VISUALS: dict[tuple[Source, bool], tuple[str, str]] = {
    (Source.GIT, True): ("●", "green"),
    (Source.FILESYSTEM, True): ("■", "bright_blue"),
    (Source.GIT, False): ("○", "bold bright_red"),
    (Source.FILESYSTEM, False): ("□", "bold bright_red"),
}


def render_chart(report: Report, options: TerminalOptions) -> Iterable[Text]:
    aggregation = report.aggregation
    time_width, day_width = chart_layout(report, options.width)
    identity_symbols = _identity_symbols(report, options)

    header = Text(pad_right("Time band", time_width), style="bold")
    for weekday in aggregation.visible_weekdays:
        _append_column_separator(header, options)
        header.append(center(WEEKDAY_LABELS[int(weekday)], day_width), style="bold")
    yield header
    if options.grid_style.has_horizontal_lines:
        yield _horizontal_rule(
            time_width=time_width,
            day_width=day_width,
            day_count=len(aggregation.visible_weekdays),
            options=options,
        )

    if not aggregation.clusters:
        if not aggregation.visible_weekdays:
            message = "No occupied days."
        elif aggregation.event_count:
            message = "No events in the displayed weekday/time range."
        else:
            message = "No events in selected scope."
        yield Text(message, style="dim")
        return

    previous: TimeCluster | None = None
    for cluster in aggregation.clusters:
        if previous is not None:
            gap_ns = cluster.start_time_ns - previous.end_time_ns
            if options.grid_style.has_horizontal_lines:
                gap_label = f"⋮ {_format_ns_duration(gap_ns)}" if gap_ns >= GAP_THRESHOLD_NS else None
                yield _horizontal_rule(
                    time_width=time_width,
                    day_width=day_width,
                    day_count=len(aggregation.visible_weekdays),
                    options=options,
                    label=gap_label,
                )
            elif gap_ns >= GAP_THRESHOLD_NS:
                yield _gap_line(
                    f"⋮ {_format_ns_duration(gap_ns)}",
                    time_width=time_width,
                    day_width=day_width,
                    day_count=len(aggregation.visible_weekdays),
                    options=options,
                )
        yield from _cluster_lines(
            cluster,
            label=_cluster_label(cluster),
            time_width=time_width,
            day_width=day_width,
            options=options,
            identity_symbols=identity_symbols,
            weekdays=aggregation.visible_weekdays,
        )
        previous = cluster


def render_legend(report: Report, options: TerminalOptions) -> tuple[Text, ...]:
    aggregation = report.aggregation
    width = options.width
    identity_symbols = _identity_symbols(report, options)
    identity_schedules = _identity_schedule_counts(report)
    inside_sources = {
        source for source in (Source.GIT, Source.FILESYSTEM) if aggregation.count_for_visual(source, True)
    }
    outside_sources = {
        source for source in (Source.GIT, Source.FILESYSTEM) if aggregation.count_for_visual(source, False)
    }
    visible_sources = inside_sources | outside_sources

    items: list[Text] = []
    if options.marker_style is MarkerStyle.IDENTITY:
        items.extend(
            _identity_legend_item(symbol)
            for symbol in identity_symbols
            if identity_schedules[(symbol.identity_id, True)] or identity_schedules[(symbol.identity_id, False)]
        )
        if Source.FILESYSTEM in visible_sources:
            items.append(_source_legend_item(Source.FILESYSTEM, within_schedule=True))
    else:
        for source in (Source.GIT, Source.FILESYSTEM):
            if source in visible_sources:
                items.append(_source_legend_item(source, within_schedule=True))

    if outside_sources:
        items.append(
            _outside_legend_item(
                outside_sources,
                options=options,
                identity_symbols=identity_symbols,
                identity_schedules=identity_schedules,
            )
        )

    _time_width, day_width = chart_layout(report, width)
    if any(
        not _literal_cell_fits(cell, day_width, options, identity_symbols)
        for cluster in aggregation.clusters
        for cell in cluster.cells
    ):
        items.append(Text("×N exact count", style="dim"))

    lines = list(_pack_legend_items(items, width))
    schedule = f"Working hours: {sanitize_terminal_text(report.context.schedule_label)}"
    lines.extend(Text(chunk) for chunk in column_chunks(schedule, width))
    return tuple(lines)


def chart_layout(report: Report, width: int) -> tuple[int, int]:
    label_width = 11 if report.aggregation.has_multi_minute_cluster else 5
    time_width = max(label_width, display_width("Time band"))
    day_count = len(report.aggregation.visible_weekdays)
    day_width = max(3, (width - time_width - day_count) // day_count) if day_count else 0
    return time_width, day_width


def _source_legend_item(source: Source, *, within_schedule: bool) -> Text:
    symbol, style = EVENT_VISUALS[(source, within_schedule)]
    label = "Git" if source is Source.GIT else "Filesystem"
    return Text(f"{symbol} {label}", style=style)


def _outside_legend_item(
    sources: set[Source],
    *,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
    identity_schedules: Counter[tuple[int, bool]],
) -> Text:
    markers: list[str] = []
    if Source.GIT in sources:
        if options.marker_style is MarkerStyle.IDENTITY:
            outside_identities = tuple(
                symbol for symbol in identity_symbols if identity_schedules[(symbol.identity_id, False)]
            )
            if any(len(symbol.identity.members) == 1 for symbol in outside_identities):
                markers.append("a–z")
            if any(len(symbol.identity.members) > 1 for symbol in outside_identities):
                markers.append("◇")
        else:
            markers.append(EVENT_VISUALS[(Source.GIT, False)][0])
    if Source.FILESYSTEM in sources:
        markers.append(EVENT_VISUALS[(Source.FILESYSTEM, False)][0])

    return Text(f"{'/'.join(markers)} Outside working hours", style="bold bright_red")


def _pack_legend_items(items: list[Text], width: int) -> tuple[Text, ...]:
    if not items:
        return ()
    separator = Text(" · ", style="dim")
    lines: list[Text] = []
    current = Text()
    for item in items:
        if item.cell_len > width:
            if current:
                lines.append(current)
                current = Text()
            lines.extend(rich_text_chunks(item, width))
            continue
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
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
    weekdays: tuple[Weekday, ...],
) -> tuple[Text, ...]:
    cells = tuple(_cell_lines(cluster.cell(weekday), day_width, options, identity_symbols) for weekday in weekdays)
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
            _append_column_separator(line, options)
            content = cell_lines[line_index] if line_index < len(cell_lines) else Text()
            line.append_text(content)
            line.append(" " * max(0, day_width - content.cell_len))
        rendered.append(line)
    return tuple(rendered)


def _append_column_separator(line: Text, options: TerminalOptions) -> None:
    if options.grid_style.has_vertical_lines:
        line.append("│", style=GRID_STYLE)
    else:
        line.append(" ")


def _horizontal_rule(
    *,
    time_width: int,
    day_width: int,
    day_count: int,
    options: TerminalOptions,
    label: str | None = None,
) -> Text:
    rule = Text(_horizontal_time_segment(time_width, label), style=GRID_STYLE)
    for _day_index in range(day_count):
        if options.grid_style.has_vertical_lines:
            rule.append("┼", style=GRID_STYLE)
        else:
            rule.append("─", style=GRID_STYLE)
        rule.append("─" * day_width, style=GRID_STYLE)
    return rule


def _horizontal_time_segment(width: int, label: str | None) -> str:
    if label is None:
        return "─" * width
    label_width = display_width(label)
    if label_width > width:
        raise ValueError("horizontal grid label exceeds the time column")
    left_width = (width - label_width) // 2
    return "─" * left_width + label + "─" * (width - label_width - left_width)


def _gap_line(
    label: str,
    *,
    time_width: int,
    day_width: int,
    day_count: int,
    options: TerminalOptions,
) -> Text:
    line = Text(pad_right(label, time_width), style=GRID_STYLE)
    if not options.grid_style.has_vertical_lines:
        return line
    for _day_index in range(day_count):
        line.append("│", style=GRID_STYLE)
        line.append(" " * day_width)
    return line


def _cell_lines(
    cell: ClusterCell | None,
    width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> tuple[Text, ...]:
    if cell is None:
        return (Text(),)
    if _literal_cell_fits(cell, width, options, identity_symbols):
        return _literal_cell_lines(cell.runs, width, options, identity_symbols)
    return _compact_cell_lines(cell.runs, width, options, identity_symbols)


def _literal_cell_fits(
    cell: ClusterCell,
    width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> bool:
    if cell.compacted:
        return False
    total_width = sum(display_width(_marker_visual(run, options, identity_symbols)[0]) * run.count for run in cell.runs)
    if total_width > width * MAX_LITERAL_EVENT_LINES:
        return False
    return len(_literal_cell_lines(cell.runs, width, options, identity_symbols)) <= MAX_LITERAL_EVENT_LINES


def _literal_cell_lines(
    runs: tuple[MarkerRun, ...],
    width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> tuple[Text, ...]:
    lines: list[Text] = []
    current = Text()
    for run in runs:
        symbol, style = _marker_visual(run, options, identity_symbols)
        symbol_width = display_width(symbol)
        for _event_index in range(run.count):
            if current and current.cell_len + symbol_width > width:
                lines.append(current)
                current = Text()
            current.append(symbol, style=style)
    if current:
        lines.append(current)
    return tuple(lines)


def _compact_cell_lines(
    runs: tuple[MarkerRun, ...],
    width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> tuple[Text, ...]:
    counts: Counter[tuple[Source, bool, int | None]] = Counter()
    for run in runs:
        counts[(run.source, run.within_schedule, run.identity_id)] += run.count
    lines: list[Text] = []
    current = Text()
    for source, within_schedule, identity_id in sorted(counts, key=_visual_sort_key):
        count = counts[(source, within_schedule, identity_id)]
        run = MarkerRun(source, within_schedule, count, identity_id)
        symbol, style = _marker_visual(run, options, identity_symbols)
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


def _marker_visual(
    run: MarkerRun,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> tuple[str, str]:
    if options.marker_style is MarkerStyle.IDENTITY and run.source is Source.GIT:
        if run.identity_id is None or not 0 <= run.identity_id < len(identity_symbols):
            raise ValueError("identity marker rendering requires a valid Git identity ID")
        identity = identity_symbols[run.identity_id]
        style = EVENT_VISUALS[(Source.GIT, run.within_schedule)][1]
        return identity.marker(within_schedule=run.within_schedule), style
    return EVENT_VISUALS[(run.source, run.within_schedule)]


def _visual_sort_key(key: tuple[Source, bool, int | None]) -> tuple[int, int]:
    source, within_schedule, identity_id = key
    visual_order = {
        (Source.GIT, True): 0,
        (Source.FILESYSTEM, True): 1,
        (Source.GIT, False): 2,
        (Source.FILESYSTEM, False): 3,
    }
    return visual_order[(source, within_schedule)], -1 if identity_id is None else identity_id


def _identity_symbols(report: Report, options: TerminalOptions) -> tuple[IdentitySymbol, ...]:
    if options.marker_style is not MarkerStyle.IDENTITY:
        return ()
    return allocate_identity_symbols(report.aggregation.identities)


def _identity_schedule_counts(report: Report) -> Counter[tuple[int, bool]]:
    counts: Counter[tuple[int, bool]] = Counter()
    for cluster in report.aggregation.clusters:
        for cell in cluster.cells:
            for run in cell.runs:
                if run.source is Source.GIT and run.identity_id is not None:
                    counts[(run.identity_id, run.within_schedule)] += run.count
    return counts


def _identity_legend_item(
    symbol: IdentitySymbol,
) -> Text:
    item = Text()
    item.append(symbol.code, style=EVENT_VISUALS[(Source.GIT, True)][1])
    item.append(f" {sanitize_terminal_text(marker_identity_label(symbol.identity))}")
    return item


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
