"""Terminal presentation options and environment policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from workfold.config import GridStyle, MarkerStyle


@dataclass(frozen=True, slots=True)
class TerminalOptions:
    """Presentation-only options resolved by the CLI environment adapter."""

    width: int = 80
    color: bool = False
    list_outside: bool = False
    verbose: bool = False
    marker_style: MarkerStyle = MarkerStyle.SOURCE
    grid_style: GridStyle = GridStyle.NONE

    def __post_init__(self) -> None:
        if self.width < 60:
            raise ValueError("terminal width must be at least 60 columns")


def terminal_color_enabled(
    *,
    no_color: bool,
    environ: Mapping[str, str],
    stdout_is_tty: bool,
) -> bool:
    """Resolve color policy from flags and process capabilities."""

    return not no_color and "NO_COLOR" not in environ and environ.get("TERM", "").casefold() != "dumb" and stdout_is_tty
