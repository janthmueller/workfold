"""Responsive terminal legend and working-schedule presentation."""

from __future__ import annotations

from collections import Counter

from rich.text import Text

from workfold.application.report import Report
from workfold.configuration.options import MarkerStyle
from workfold.domain.observations import Source
from workfold.reporting.sanitization import sanitize_terminal_text
from workfold.reporting.terminal.chart import literal_cell_fits
from workfold.reporting.terminal.chart_time import chart_layout
from workfold.reporting.terminal.identity import IdentitySymbol, marker_identity_label
from workfold.reporting.terminal.layout import column_chunks, rich_text_chunks
from workfold.reporting.terminal.markers import EVENT_VISUALS, identity_symbols
from workfold.reporting.terminal.options import TerminalOptions


def render_legend(report: Report, options: TerminalOptions) -> tuple[Text, ...]:
    """Render only marker forms present in the chart, followed by schedule."""

    aggregation = report.aggregation
    symbols = identity_symbols(report, options)
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
            for symbol in symbols
            if identity_schedules[(symbol.identity_id, True)] or identity_schedules[(symbol.identity_id, False)]
        )
        if Source.FILESYSTEM in visible_sources:
            items.append(_source_legend_item(Source.FILESYSTEM))
        if aggregation.identity_overflow and Source.GIT in visible_sources:
            items.append(_source_legend_item(Source.GIT))
            items.append(Text("Identity view grouped into Git markers (identity limit exceeded)", style="dim"))
    else:
        for source in (Source.GIT, Source.FILESYSTEM):
            if source in visible_sources:
                items.append(_source_legend_item(source))

    if outside_sources:
        items.append(
            _outside_legend_item(
                outside_sources,
                options=options,
                identity_symbols=symbols,
                identity_schedules=identity_schedules,
            )
        )

    _time_width, day_width = chart_layout(report, options)
    if any(
        not literal_cell_fits(cell, day_width, options, symbols)
        for cluster in aggregation.clusters
        for cell in cluster.cells
    ):
        items.append(Text("×N exact count", style="dim"))

    lines = list(_pack_legend_items(items, options.width))
    schedule = f"Working hours: {sanitize_terminal_text(report.context.scope.schedule)}"
    lines.extend(Text(chunk) for chunk in column_chunks(schedule, options.width))
    return tuple(lines)


def _source_legend_item(source: Source) -> Text:
    symbol, style = EVENT_VISUALS[(source, True)]
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
            if not identity_symbols:
                markers.append(EVENT_VISUALS[(Source.GIT, False)][0])
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


def _identity_schedule_counts(report: Report) -> Counter[tuple[int, bool]]:
    counts: Counter[tuple[int, bool]] = Counter()
    for cluster in report.aggregation.clusters:
        for cell in cluster.cells:
            for run in cell.runs:
                if run.source is Source.GIT and run.identity_id is not None:
                    counts[(run.identity_id, run.within_schedule)] += run.count
    return counts


def _identity_legend_item(symbol: IdentitySymbol) -> Text:
    item = Text()
    item.append(symbol.code, style=EVENT_VISUALS[(Source.GIT, True)][1])
    item.append(f" {sanitize_terminal_text(marker_identity_label(symbol.identity))}")
    return item


__all__ = ["render_legend"]
