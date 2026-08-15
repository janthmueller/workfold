"""Top-level incremental terminal report composition."""

from __future__ import annotations

from collections.abc import Iterable
from io import StringIO
from typing import TextIO

from rich.console import Console
from rich.text import Text

from workfold.application.report import Report
from workfold.reporting.terminal.chart import render_chart
from workfold.reporting.terminal.event_list import render_event_list
from workfold.reporting.terminal.layout import plain_section
from workfold.reporting.terminal.legend import render_legend
from workfold.reporting.terminal.options import TerminalOptions
from workfold.reporting.terminal.summary import render_details, render_summary


def render_terminal(report: Report, *, options: TerminalOptions | None = None) -> str:
    """Render *report* as terminal text ending in exactly one newline."""

    stream = StringIO()
    write_terminal(report, stream, options=options)
    return stream.getvalue().rstrip("\n") + "\n"


def write_terminal(
    report: Report,
    stream: TextIO,
    *,
    options: TerminalOptions | None = None,
) -> None:
    """Write *report* incrementally without retaining the complete output."""

    resolved = options or TerminalOptions()
    console = Console(
        file=stream,
        width=resolved.width,
        color_system="standard" if resolved.color else None,
        force_terminal=resolved.color,
        no_color=not resolved.color,
        highlight=False,
        legacy_windows=False,
    )
    sections: list[Iterable[Text]] = [
        render_chart(report, resolved),
        render_legend(report, resolved),
        plain_section(render_summary(report, resolved)),
    ]
    if resolved.verbose:
        sections.append(
            plain_section(
                render_details(
                    report,
                    resolved.width,
                    band_label=resolved.band_label,
                    show_empty_bands=resolved.show_empty_bands,
                ),
                heading=True,
            )
        )
    if resolved.show_event_list:
        sections.append(plain_section(render_event_list(report, resolved.width), heading=True))

    wrote_section = False
    for section in sections:
        iterator = iter(section)
        first = next(iterator, None)
        if first is None:
            continue
        if wrote_section:
            console.print()
        console.print(first, soft_wrap=False)
        for line in iterator:
            console.print(line, soft_wrap=False)
        wrote_section = True
