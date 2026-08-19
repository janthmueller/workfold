"""Terminal marker allocation and source/schedule visuals."""

from __future__ import annotations

from workfold.application.report import Report
from workfold.configuration.options import MarkerStyle
from workfold.configuration.styles import DEFAULT_EVENT_STYLE_SHEET
from workfold.domain.evidence import EvidenceKind, evidence_mask
from workfold.domain.observations import Source
from workfold.folding import MarkerRun
from workfold.reporting.terminal.identity import IdentitySymbol, allocate_identity_symbols
from workfold.reporting.terminal.options import TerminalOptions

_DEFAULT_SOURCE_MASKS = {
    Source.GIT: evidence_mask((EvidenceKind.GIT_COMMIT_AUTHOR,)),
    Source.FILESYSTEM: evidence_mask((EvidenceKind.FS_FILE_MODIFIED,)),
}


def _default_event_visual(source: Source, *, within_schedule: bool) -> tuple[str, str]:
    visual = DEFAULT_EVENT_STYLE_SHEET.visual_for(_DEFAULT_SOURCE_MASKS[source], within_schedule=within_schedule)
    style = visual.color if within_schedule else f"bold {visual.color}"
    return visual.symbol, style


EVENT_VISUALS: dict[tuple[Source, bool], tuple[str, str]] = {
    (source, within_schedule): _default_event_visual(source, within_schedule=within_schedule)
    for source in Source
    for within_schedule in (True, False)
}


def marker_visual(
    run: MarkerRun,
    options: TerminalOptions,
    identity_symbols: tuple[IdentitySymbol, ...],
) -> tuple[str, str]:
    """Resolve one marker run to its printable symbol and Rich style."""

    if options.marker_style is MarkerStyle.IDENTITY and run.source is Source.GIT:
        if run.identity_id is None and not identity_symbols:
            return EVENT_VISUALS[(run.source, run.within_schedule)]
        if run.identity_id is None or not 0 <= run.identity_id < len(identity_symbols):
            raise ValueError("identity marker rendering requires a valid Git identity ID")
        identity = identity_symbols[run.identity_id]
        style = EVENT_VISUALS[(Source.GIT, run.within_schedule)][1]
        return identity.marker(within_schedule=run.within_schedule), style
    visual = options.event_styles.visual_for(run.evidence_mask, within_schedule=run.within_schedule)
    style = f"bold {visual.color}" if not run.within_schedule else visual.color
    return visual.symbol, style


def visual_sort_key(key: tuple[Source, bool, int, int | None]) -> tuple[int, int, int]:
    """Keep compact source/schedule tokens in stable visual order."""

    source, within_schedule, evidence_mask, identity_id = key
    visual_order = {
        (Source.GIT, True): 0,
        (Source.FILESYSTEM, True): 1,
        (Source.GIT, False): 2,
        (Source.FILESYSTEM, False): 3,
    }
    return visual_order[(source, within_schedule)], evidence_mask, -1 if identity_id is None else identity_id


def identity_symbols(report: Report, options: TerminalOptions) -> tuple[IdentitySymbol, ...]:
    """Allocate identity markers only when that presentation mode is active."""

    if options.marker_style is not MarkerStyle.IDENTITY:
        return ()
    return allocate_identity_symbols(report.aggregation.identities)


__all__ = ["EVENT_VISUALS", "identity_symbols", "marker_visual", "visual_sort_key"]
