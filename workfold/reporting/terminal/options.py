"""Terminal presentation options and environment policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from workfold.configuration.options import BandLabel, GridStyle, MarkerStyle


@dataclass(frozen=True, slots=True)
class TerminalOptions:
    """Presentation-only options resolved by the CLI environment adapter."""

    width: int = 80
    color: bool = False
    show_event_list: bool = False
    verbose: bool = False
    marker_style: MarkerStyle = MarkerStyle.SOURCE
    grid_style: GridStyle = GridStyle.NONE
    band_label: BandLabel = BandLabel.RANGE
    show_empty_bands: bool = False
    coverage: bool = False

    def __post_init__(self) -> None:
        if self.width < 60:
            raise ValueError("terminal width must be at least 60 columns")
        _require_option_type(self.marker_style, MarkerStyle, "marker_style")
        _require_option_type(self.grid_style, GridStyle, "grid_style")
        _require_option_type(self.band_label, BandLabel, "band_label")
        _require_option_type(self.show_event_list, bool, "show_event_list")
        _require_option_type(self.show_empty_bands, bool, "show_empty_bands")
        _require_option_type(self.coverage, bool, "coverage")


def _require_option_type(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be a {expected.__name__}")


def terminal_color_enabled(
    *,
    no_color: bool,
    environ: Mapping[str, str],
    stdout_is_tty: bool,
) -> bool:
    """Resolve color policy from flags and process capabilities."""

    return not no_color and "NO_COLOR" not in environ and environ.get("TERM", "").casefold() != "dumb" and stdout_is_tty
