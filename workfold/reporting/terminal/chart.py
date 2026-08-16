"""Weekly terminal chart with sparse/dense time-band presentation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from rich.text import Text

from workfold.application.report import Report
from workfold.configuration.options import BandLabel
from workfold.domain.observations import Source, Weekday
from workfold.folding import (
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_SECOND,
    Aggregation,
    ClusterCell,
    MarkerRun,
    TimeCluster,
)
from workfold.folding.bands import ClusterAnchor, duration_nanoseconds
from workfold.reporting.sanitization import display_width, pad_right, truncate_end
from workfold.reporting.terminal.chart_time import (
    chart_layout,
    cluster_label,
    fixed_band_label,
    gap_cue_label,
    time_heading,
)
from workfold.reporting.terminal.identity import IdentitySymbol
from workfold.reporting.terminal.layout import center
from workfold.reporting.terminal.markers import identity_symbols, marker_visual, visual_sort_key
from workfold.reporting.terminal.options import TerminalOptions

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_LITERAL_EVENT_LINES = 3
GRID_STYLE = "dim"


def render_chart(report: Report, options: TerminalOptions) -> Iterable[Text]:
    aggregation = report.aggregation
    if options.show_empty_bands and aggregation.cluster_anchor is not ClusterAnchor.MIDNIGHT:
        raise ValueError("show_empty_bands requires midnight-anchored clusters")
    time_width, day_width = chart_layout(report, options)
    symbols = identity_symbols(report, options)
    gap_threshold_ns = duration_nanoseconds(aggregation.cluster_window)

    header = Text(pad_right(time_heading(options), time_width), style="bold")
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

    if not aggregation.visible_weekdays:
        yield Text("No occupied days.", style="dim")
        return

    if options.show_empty_bands:
        yield from _dense_band_lines(
            aggregation,
            time_width=time_width,
            day_width=day_width,
            options=options,
            identity_symbols=symbols,
        )
        return

    if not aggregation.clusters:
        if aggregation.event_count:
            message = "No events in the displayed weekday/time range."
        else:
            message = "No events in selected scope."
        yield Text(message, style="dim")
        return

    previous: TimeCluster | None = None
    for cluster in aggregation.clusters:
        if previous is not None:
            gap_label = gap_cue_label(previous, cluster, aggregation.cluster_anchor, gap_threshold_ns)
            if options.grid_style.has_horizontal_lines:
                yield _horizontal_rule(
                    time_width=time_width,
                    day_width=day_width,
                    day_count=len(aggregation.visible_weekdays),
                    options=options,
                    label=gap_label,
                )
            elif gap_label is not None:
                yield _gap_line(
                    gap_label,
                    time_width=time_width,
                    day_width=day_width,
                    day_count=len(aggregation.visible_weekdays),
                    options=options,
                )
        yield from _cluster_lines(
            cluster,
            label=cluster_label(cluster, aggregation.cluster_anchor, options.band_label),
            time_width=time_width,
            day_width=day_width,
            options=options,
            identity_symbols=symbols,
            weekdays=aggregation.visible_weekdays,
        )
        previous = cluster


def _dense_band_lines(
    aggregation: Aggregation,
    *,
    time_width: int,
    day_width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> Iterable[Text]:
    previous_row = False
    for band_start_ns, band_end_ns, cluster, is_clipped in _dense_band_rows(aggregation):
        if previous_row and options.grid_style.has_horizontal_lines:
            yield _horizontal_rule(
                time_width=time_width,
                day_width=day_width,
                day_count=len(aggregation.visible_weekdays),
                options=options,
            )
        label_style = BandLabel.RANGE if is_clipped else options.band_label
        label = fixed_band_label(band_start_ns, band_end_ns, label_style)
        if cluster is None:
            yield _empty_band_line(
                label,
                time_width=time_width,
                day_width=day_width,
                options=options,
                day_count=len(aggregation.visible_weekdays),
            )
        else:
            yield from _cluster_lines(
                cluster,
                label=label,
                time_width=time_width,
                day_width=day_width,
                options=options,
                identity_symbols=identity_symbols,
                weekdays=aggregation.visible_weekdays,
            )
        previous_row = True


def _dense_band_rows(aggregation: Aggregation) -> Iterable[tuple[int, int, TimeCluster | None, bool]]:
    window_ns = duration_nanoseconds(aggregation.cluster_window)
    display_start_ns = aggregation.display_start_minute * 60 * NANOSECONDS_PER_SECOND
    display_end_ns = aggregation.display_end_minute * 60 * NANOSECONDS_PER_SECOND
    band_start_ns = display_start_ns // window_ns * window_ns
    clusters = iter(aggregation.clusters)
    current = next(clusters, None)

    while band_start_ns < display_end_ns:
        band_end_ns = min(band_start_ns + window_ns, NANOSECONDS_PER_DAY)
        cluster = None
        if current is not None and current.band_start_time_ns == band_start_ns:
            cluster = current
            current = next(clusters, None)
        visible_start_ns = max(band_start_ns, display_start_ns)
        visible_end_ns = min(band_end_ns, display_end_ns)
        if visible_start_ns < visible_end_ns:
            is_clipped = visible_start_ns != band_start_ns or visible_end_ns != band_end_ns
            yield visible_start_ns, visible_end_ns, cluster, is_clipped
        band_start_ns = band_end_ns


def _empty_band_line(
    label: str,
    *,
    time_width: int,
    day_width: int,
    day_count: int,
    options: TerminalOptions,
) -> Text:
    line = Text(pad_right(label, time_width), style="dim")
    for _day_index in range(day_count):
        _append_column_separator(line, options)
        line.append(" " * day_width)
    return line


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
    label = truncate_end(label, width)
    label_width = display_width(label)
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
    if literal_cell_fits(cell, width, options, identity_symbols):
        return _literal_cell_lines(cell.runs, width, options, identity_symbols)
    return _compact_cell_lines(cell.runs, width, options, identity_symbols)


def literal_cell_fits(
    cell: ClusterCell,
    width: int,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> bool:
    if cell.compacted:
        return False
    total_width = sum(display_width(marker_visual(run, options, identity_symbols)[0]) * run.count for run in cell.runs)
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
        symbol, style = marker_visual(run, options, identity_symbols)
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
    for source, within_schedule, identity_id in sorted(counts, key=visual_sort_key):
        count = counts[(source, within_schedule, identity_id)]
        run = MarkerRun(source, within_schedule, count, identity_id)
        symbol, style = marker_visual(run, options, identity_symbols)
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
