"""Terminal marker allocation and source/schedule visuals."""

from __future__ import annotations

from workfold.application.report import Report
from workfold.configuration.options import MarkerStyle
from workfold.domain.observations import Source
from workfold.folding import MarkerRun
from workfold.reporting.terminal.identity import IdentitySymbol, allocate_identity_symbols
from workfold.reporting.terminal.options import TerminalOptions

EVENT_VISUALS: dict[tuple[Source, bool], tuple[str, str]] = {
    (Source.GIT, True): ("●", "green"),
    (Source.FILESYSTEM, True): ("■", "bright_blue"),
    (Source.GIT, False): ("○", "bold bright_red"),
    (Source.FILESYSTEM, False): ("□", "bold bright_red"),
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
    return EVENT_VISUALS[(run.source, run.within_schedule)]


def visual_sort_key(key: tuple[Source, bool, int | None]) -> tuple[int, int]:
    """Keep compact source/schedule tokens in stable visual order."""

    source, within_schedule, identity_id = key
    visual_order = {
        (Source.GIT, True): 0,
        (Source.FILESYSTEM, True): 1,
        (Source.GIT, False): 2,
        (Source.FILESYSTEM, False): 3,
    }
    return visual_order[(source, within_schedule)], -1 if identity_id is None else identity_id


def identity_symbols(report: Report, options: TerminalOptions) -> tuple[IdentitySymbol, ...]:
    """Allocate identity markers only when that presentation mode is active."""

    if options.marker_style is not MarkerStyle.IDENTITY:
        return ()
    return allocate_identity_symbols(report.aggregation.identities)


__all__ = ["EVENT_VISUALS", "identity_symbols", "marker_visual", "visual_sort_key"]
