"""Terminal chart time labels, gap cues, and column sizing."""

from __future__ import annotations

from workfold.application.report import Report
from workfold.configuration.options import BandLabel
from workfold.folding import (
    NANOSECONDS_PER_DAY,
    NANOSECONDS_PER_MINUTE,
    NANOSECONDS_PER_SECOND,
    Aggregation,
    TimeCluster,
)
from workfold.folding.bands import ClusterAnchor, duration_nanoseconds
from workfold.reporting.sanitization import display_width
from workfold.reporting.terminal.options import TerminalOptions


def chart_layout(report: Report, options: TerminalOptions) -> tuple[int, int]:
    """Resolve time-label and weekday-column widths for the terminal."""

    aggregation = report.aggregation
    if options.band_label is BandLabel.START:
        label_width = 11 if _dense_edges_are_clipped(aggregation, options) else 5
    elif aggregation.cluster_anchor is ClusterAnchor.MIDNIGHT or aggregation.has_multi_minute_cluster:
        label_width = 11
    else:
        label_width = 5
    time_width = max(
        label_width,
        display_width(time_heading(options)),
        0 if options.show_empty_bands else _maximum_gap_label_width(aggregation),
    )
    day_count = len(aggregation.visible_weekdays)
    day_width = max(3, (options.width - time_width - day_count) // day_count) if day_count else 0
    return time_width, day_width


def time_heading(options: TerminalOptions) -> str:
    return "Time" if options.band_label is BandLabel.START else "Time band"


def cluster_label(cluster: TimeCluster, anchor: ClusterAnchor, label: BandLabel) -> str:
    start = _format_time_ns(cluster.band_start_time_ns)
    if label is BandLabel.START:
        return start
    if anchor is ClusterAnchor.MIDNIGHT:
        return f"{start}–{_format_time_ns(cluster.band_end_time_ns)}"
    start = _format_time_ns(cluster.observed_start_time_ns)
    end = _format_time_ns(cluster.observed_end_time_ns)
    return start if start == end else f"{start}–{end}"


def fixed_band_label(start_time_ns: int, end_time_ns: int, label: BandLabel) -> str:
    start = _format_time_ns(start_time_ns)
    return start if label is BandLabel.START else f"{start}–{_format_time_ns(end_time_ns)}"


def gap_cue_label(
    previous: TimeCluster,
    current: TimeCluster,
    anchor: ClusterAnchor,
    threshold_ns: int,
) -> str | None:
    """Describe a compressed gap once it reaches the configured threshold."""

    gap_ns = _cluster_gap_ns(previous, current, anchor)
    # Match the cue's precision to the configured window. Exact seconds remain
    # meaningful for second-based windows but are noise for minute-based charts.
    show_seconds = bool(threshold_ns % NANOSECONDS_PER_MINUTE)
    show_fraction = bool(threshold_ns % NANOSECONDS_PER_SECOND)
    return (
        f"⋮ {_format_ns_duration(gap_ns, show_seconds=show_seconds, show_fraction=show_fraction)}"
        if gap_ns >= threshold_ns
        else None
    )


def _dense_edges_are_clipped(aggregation: Aggregation, options: TerminalOptions) -> bool:
    if not options.show_empty_bands or not aggregation.display_is_explicit:
        return False
    window_ns = duration_nanoseconds(aggregation.cluster_window)
    display_start_ns = aggregation.display_start_minute * 60 * NANOSECONDS_PER_SECOND
    display_end_ns = aggregation.display_end_minute * 60 * NANOSECONDS_PER_SECOND
    return bool(display_start_ns % window_ns or (display_end_ns != NANOSECONDS_PER_DAY and display_end_ns % window_ns))


def _cluster_gap_ns(previous: TimeCluster, current: TimeCluster, anchor: ClusterAnchor) -> int:
    if anchor is ClusterAnchor.MIDNIGHT:
        return current.band_start_time_ns - previous.band_end_time_ns
    return current.observed_start_time_ns - previous.observed_end_time_ns


def _maximum_gap_label_width(aggregation: Aggregation) -> int:
    threshold_ns = duration_nanoseconds(aggregation.cluster_window)
    maximum = 0
    previous: TimeCluster | None = None
    for cluster in aggregation.clusters:
        if previous is not None:
            label = gap_cue_label(previous, cluster, aggregation.cluster_anchor, threshold_ns)
            if label is not None:
                maximum = max(maximum, display_width(label))
        previous = cluster
    return maximum


def _format_time_ns(time_ns: int) -> str:
    total_seconds = time_ns // NANOSECONDS_PER_SECOND
    hour, remainder = divmod(total_seconds, 3_600)
    minute = remainder // 60
    return f"{hour:02d}:{minute:02d}"


def _format_ns_duration(
    value_ns: int,
    *,
    show_seconds: bool = False,
    show_fraction: bool = False,
) -> str:
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
    elif seconds and (show_seconds or not parts):
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


__all__ = ["chart_layout", "cluster_label", "fixed_band_label", "gap_cue_label", "time_heading"]
