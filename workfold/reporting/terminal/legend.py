"""Responsive terminal legend and working-schedule presentation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rich.text import Text

from workfold.application.report import Report
from workfold.configuration.options import CountGrouping, MarkerStyle
from workfold.configuration.styles import DEFAULT_EVENT_STYLE_SHEET, EventVisualStyle
from workfold.domain.evidence import EvidenceKind, evidence_kinds_from_mask, evidence_mask_source
from workfold.domain.observations import EntryType, RecordKind, Source
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
            items.extend(_source_style_legend_items(report, options, Source.FILESYSTEM))
        if aggregation.identity_overflow and Source.GIT in visible_sources:
            items.append(_default_source_legend_item(Source.GIT))
            items.append(Text("Identity view grouped into Git markers (identity limit exceeded)", style="dim"))
    else:
        for source in (Source.GIT, Source.FILESYSTEM):
            if source in visible_sources:
                items.extend(_source_style_legend_items(report, options, source))

    if outside_sources:
        items.append(
            _outside_legend_item(
                outside_sources,
                report=report,
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
        label = "×N exact per visual" if options.count_grouping is CountGrouping.VISUAL else "×N exact per event kind"
        items.append(Text(label, style="dim"))

    lines = list(_pack_legend_items(items, options.width))
    schedule = f"Working hours: {sanitize_terminal_text(report.context.scope.schedule)}"
    lines.extend(Text(chunk) for chunk in column_chunks(schedule, options.width))
    return tuple(lines)


def _default_source_legend_item(source: Source) -> Text:
    symbol, style = EVENT_VISUALS[(source, True)]
    label = "Git" if source is Source.GIT else "Filesystem"
    return Text(f"{symbol} {label}", style=style)


@dataclass(frozen=True, slots=True)
class _StyleGroup:
    source: Source
    style: EventVisualStyle
    masks: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _LegendToken:
    symbol: str
    color: str


def _source_style_legend_items(report: Report, options: TerminalOptions, source: Source) -> tuple[Text, ...]:
    groups = _visible_style_groups(report, options, source)
    family_groups: Counter[tuple[RecordKind, EntryType | None]] = Counter()
    for group in groups:
        family_groups.update({_evidence_family(mask) for mask in group.masks})
    sheet_style_count = len(
        {visual for mask, visual in options.event_styles.visuals if evidence_mask_source(mask) is source}
    )
    items: list[Text] = []
    for group in groups:
        label = _style_group_label(
            group,
            group_count=len(groups),
            sheet_style_count=sheet_style_count,
            family_groups=family_groups,
        )
        items.append(Text(f"{group.style.inside.symbol} {label}", style=group.style.inside.color))
    return tuple(items)


def _visible_style_groups(report: Report, options: TerminalOptions, source: Source) -> tuple[_StyleGroup, ...]:
    visible_masks = {
        mask
        for (mask, _within_schedule), count in report.aggregation.visual_counts
        if count and evidence_mask_source(mask) is source
    }
    grouped: dict[EventVisualStyle, list[int]] = {}
    for mask in sorted(visible_masks):
        grouped.setdefault(options.event_styles.style_for(mask), []).append(mask)
    return tuple(
        _StyleGroup(source, style, tuple(masks))
        for style, masks in sorted(grouped.items(), key=lambda item: min(item[1]))
    )


def _style_group_label(
    group: _StyleGroup,
    *,
    group_count: int,
    sheet_style_count: int,
    family_groups: Counter[tuple[RecordKind, EntryType | None]],
) -> str:
    uses_builtin_visual = group.style == DEFAULT_EVENT_STYLE_SHEET.style_for(group.masks[0])
    if group_count == 1 and (sheet_style_count == 1 or uses_builtin_visual):
        return "Git" if group.source is Source.GIT else "Filesystem"
    families = tuple(dict.fromkeys(_evidence_family(mask) for mask in group.masks))
    if len(families) == 1 and family_groups[families[0]] > 1:
        return _signature_label(group.masks)
    labels = tuple(_family_label(family) for family in families)
    return " + ".join(labels)


def _evidence_family(mask: int) -> tuple[RecordKind, EntryType | None]:
    kinds = evidence_kinds_from_mask(mask)
    first = kinds[0]
    family = (first.record_kind, first.entry_type)
    if any((kind.record_kind, kind.entry_type) != family for kind in kinds[1:]):
        raise ValueError("a plotted marker cannot span event-record families")
    return family


def _family_label(family: tuple[RecordKind, EntryType | None]) -> str:
    labels: dict[tuple[RecordKind, EntryType | None], str] = {
        (RecordKind.COMMIT, None): "Git commits",
        (RecordKind.GIT_FILE_CHANGE, None): "Git file changes",
        (RecordKind.TAG, None): "Git tags",
        (RecordKind.REFLOG, None): "Git reflogs",
        (RecordKind.FILESYSTEM_ENTRY, EntryType.REGULAR_FILE): "Filesystem files",
        (RecordKind.FILESYSTEM_ENTRY, EntryType.DIRECTORY): "Filesystem directories",
        (RecordKind.FILESYSTEM_ENTRY, EntryType.SYMLINK): "Filesystem symlinks",
    }
    return labels[family]


def _signature_label(masks: tuple[int, ...]) -> str:
    kinds = tuple(dict.fromkeys(kind for mask in masks for kind in evidence_kinds_from_mask(mask)))
    if len(masks) == 1 and len(kinds) == 1:
        return _evidence_label(kinds[0])
    family = _family_label(_evidence_family(masks[0]))
    roles = " + ".join(dict.fromkeys(_evidence_role(kind) for kind in kinds))
    return f"{family} ({roles})"


def _evidence_label(kind: EvidenceKind) -> str:
    return f"{_family_label((kind.record_kind, kind.entry_type))} ({_evidence_role(kind)})"


def _evidence_role(kind: EvidenceKind) -> str:
    return kind.value.rsplit(":", maxsplit=1)[1]


def _outside_legend_item(
    sources: set[Source],
    *,
    report: Report,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
    identity_schedules: Counter[tuple[int, bool]],
) -> Text:
    markers: list[_LegendToken] = []
    if Source.GIT in sources:
        if options.marker_style is MarkerStyle.IDENTITY:
            if not identity_symbols:
                symbol, _style = EVENT_VISUALS[(Source.GIT, False)]
                markers.append(_LegendToken(symbol, "bright_red"))
            outside_identities = tuple(
                symbol for symbol in identity_symbols if identity_schedules[(symbol.identity_id, False)]
            )
            if any(len(symbol.identity.members) == 1 for symbol in outside_identities):
                markers.append(_LegendToken("a–z", "bright_red"))
            if any(len(symbol.identity.members) > 1 for symbol in outside_identities):
                markers.append(_LegendToken("◇", "bright_red"))
        else:
            markers.extend(_outside_visuals(report, source=Source.GIT, options=options))
    if Source.FILESYSTEM in sources:
        markers.extend(_outside_visuals(report, source=Source.FILESYSTEM, options=options))
    unique = tuple(dict.fromkeys(markers))
    if len({visual.color for visual in unique}) == 1:
        return Text(
            f"{'/'.join(visual.symbol for visual in unique)} Outside working hours",
            style=f"bold {unique[0].color}",
        )
    item = Text()
    for index, visual in enumerate(unique):
        if index:
            item.append("/", style="dim")
        item.append(visual.symbol, style=f"bold {visual.color}")
    item.append(" Outside working hours", style="bold")
    return item


def _outside_visuals(report: Report, *, source: Source, options: TerminalOptions) -> tuple[_LegendToken, ...]:
    masks = {
        mask
        for (mask, within_schedule), count in report.aggregation.visual_counts
        if count and not within_schedule and evidence_mask_source(mask) is source
    }
    visuals = (options.event_styles.visual_for(mask, within_schedule=False) for mask in sorted(masks))
    return tuple(dict.fromkeys(_LegendToken(visual.symbol, visual.color) for visual in visuals))


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
