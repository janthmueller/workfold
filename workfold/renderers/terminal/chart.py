"""Weekly terminal chart with sparse/dense time-band presentation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from rich.text import Text

from workfold.aggregation import (
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_SECOND,
    Aggregation,
    ClusterCell,
    MarkerRun,
    TimeCluster,
)
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
from workfold.time_bands import BandLabel, ClusterAnchor, duration_nanoseconds

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
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
    if options.show_empty_bands and aggregation.cluster_anchor is not ClusterAnchor.MIDNIGHT:
        raise ValueError("show_empty_bands requires midnight-anchored clusters")
    time_width, day_width = chart_layout(report, options)
    identity_symbols = _identity_symbols(report, options)
    gap_threshold_ns = duration_nanoseconds(aggregation.cluster_window)

    header = Text(pad_right(_time_heading(options), time_width), style="bold")
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
            identity_symbols=identity_symbols,
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
            gap_label = _gap_cue_label(previous, cluster, aggregation.cluster_anchor, gap_threshold_ns)
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
            label=_cluster_label(cluster, aggregation.cluster_anchor, options.band_label),
            time_width=time_width,
            day_width=day_width,
            options=options,
            identity_symbols=identity_symbols,
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
        label = _fixed_band_label(band_start_ns, band_end_ns, label_style)
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

    _time_width, day_width = chart_layout(report, options)
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


def chart_layout(report: Report, options: TerminalOptions) -> tuple[int, int]:
    aggregation = report.aggregation
    if options.band_label is BandLabel.START:
        label_width = 11 if _dense_edges_are_clipped(aggregation, options) else 5
    elif aggregation.cluster_anchor is ClusterAnchor.MIDNIGHT or aggregation.has_multi_minute_cluster:
        label_width = 11
    else:
        label_width = 5
    time_width = max(
        label_width,
        display_width(_time_heading(options)),
        0 if options.show_empty_bands else _maximum_gap_label_width(aggregation),
    )
    day_count = len(report.aggregation.visible_weekdays)
    day_width = max(3, (options.width - time_width - day_count) // day_count) if day_count else 0
    return time_width, day_width


def _dense_edges_are_clipped(aggregation: Aggregation, options: TerminalOptions) -> bool:
    if not options.show_empty_bands or not aggregation.display_is_explicit:
        return False
    window_ns = duration_nanoseconds(aggregation.cluster_window)
    display_start_ns = aggregation.display_start_minute * 60 * NANOSECONDS_PER_SECOND
    display_end_ns = aggregation.display_end_minute * 60 * NANOSECONDS_PER_SECOND
    return bool(display_start_ns % window_ns or (display_end_ns != NANOSECONDS_PER_DAY and display_end_ns % window_ns))


def _time_heading(options: TerminalOptions) -> str:
    return "Time" if options.band_label is BandLabel.START else "Time band"


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


def _cluster_label(cluster: TimeCluster, anchor: ClusterAnchor, label: BandLabel) -> str:
    start = _format_time_ns(cluster.band_start_time_ns)
    if label is BandLabel.START:
        return start
    if anchor is ClusterAnchor.MIDNIGHT:
        return f"{start}–{_format_time_ns(cluster.band_end_time_ns)}"
    start = _format_time_ns(cluster.observed_start_time_ns)
    end = _format_time_ns(cluster.observed_end_time_ns)
    return start if start == end else f"{start}–{end}"


def _fixed_band_label(start_time_ns: int, end_time_ns: int, label: BandLabel) -> str:
    start = _format_time_ns(start_time_ns)
    return start if label is BandLabel.START else f"{start}–{_format_time_ns(end_time_ns)}"


def _cluster_gap_ns(previous: TimeCluster, current: TimeCluster, anchor: ClusterAnchor) -> int:
    if anchor is ClusterAnchor.MIDNIGHT:
        return current.band_start_time_ns - previous.band_end_time_ns
    return current.observed_start_time_ns - previous.observed_end_time_ns


def _gap_cue_label(
    previous: TimeCluster,
    current: TimeCluster,
    anchor: ClusterAnchor,
    threshold_ns: int,
) -> str | None:
    gap_ns = _cluster_gap_ns(previous, current, anchor)
    # Normal CLI windows have whole-second precision. Preserve the compact cue
    # unless a programmatic window makes subsecond precision semantically relevant.
    show_fraction = bool(threshold_ns % NANOSECONDS_PER_SECOND)
    return f"⋮ {_format_ns_duration(gap_ns, show_fraction=show_fraction)}" if gap_ns >= threshold_ns else None


def _maximum_gap_label_width(aggregation: Aggregation) -> int:
    threshold_ns = duration_nanoseconds(aggregation.cluster_window)
    maximum = 0
    previous: TimeCluster | None = None
    for cluster in aggregation.clusters:
        if previous is not None:
            label = _gap_cue_label(previous, cluster, aggregation.cluster_anchor, threshold_ns)
            if label is not None:
                maximum = max(maximum, display_width(label))
        previous = cluster
    return maximum


def _format_time_ns(time_ns: int) -> str:
    total_seconds = time_ns // NANOSECONDS_PER_SECOND
    hour, remainder = divmod(total_seconds, 3_600)
    minute = remainder // 60
    return f"{hour:02d}:{minute:02d}"


def _format_ns_duration(value_ns: int, *, show_fraction: bool = False) -> str:
    total_seconds, fractional_ns = divmod(value_ns, NANOSECONDS_PER_SECOND)
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if fractional_ns and show_fraction:
        fractional = f"{fractional_ns:09d}".rstrip("0")
        parts.append(f"{seconds}.{fractional}s")
    elif seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"
