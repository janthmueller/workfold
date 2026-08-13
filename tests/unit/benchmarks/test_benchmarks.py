from __future__ import annotations

import pytest
from benchmarks.cases import select_cases
from benchmarks.metrics import Sample, parse_event_count
from benchmarks.run import BenchmarkError, summarize_samples


def _sample(
    wall_seconds: float,
    *,
    events: int = 12,
    digest: str = "stable",
    peak_rss: int = 100,
) -> Sample:
    return Sample(
        wall_seconds=wall_seconds,
        cpu_user_seconds=wall_seconds / 2,
        cpu_system_seconds=wall_seconds / 4,
        main_process_high_water_rss_bytes=80,
        peak_process_tree_rss_bytes=peak_rss,
        minor_page_faults=10,
        major_page_faults=0,
        voluntary_context_switches=2,
        involuntary_context_switches=1,
        exit_code=0,
        timed_out=False,
        stdout_bytes=80,
        stdout_lines=4,
        stderr_bytes=0,
        stderr_lines=0,
        stdout_sha256=digest,
        event_count=events,
        stderr_excerpt="",
    )


def test_parse_event_count_accepts_grouped_terminal_summary() -> None:
    assert parse_event_count("Time band\n\nEvents    1,284\nSchedule  ...\n") == 1_284
    assert parse_event_count("No summary here") is None


def test_select_cases_preserves_explicit_order_and_removes_duplicates() -> None:
    selected = select_cases("quick", ("fs-all", "git-current", "fs-all"))
    assert tuple(case.name for case in selected) == ("fs-all", "git-current")


def test_summarize_samples_uses_medians_and_worst_case_memory() -> None:
    summary = summarize_samples((_sample(3.0, peak_rss=120), _sample(1.0, peak_rss=100), _sample(2.0, peak_rss=110)))
    assert summary.wall_median_seconds == 2.0
    assert summary.wall_min_seconds == 1.0
    assert summary.wall_max_seconds == 3.0
    assert summary.cpu_median_seconds == 1.5
    assert summary.main_rss_median_bytes == 80
    assert summary.main_rss_max_bytes == 80
    assert summary.peak_rss_median_bytes == 110
    assert summary.peak_rss_max_bytes == 120
    assert summary.event_count == 12


@pytest.mark.parametrize(
    "samples",
    [
        (_sample(1.0, events=1), _sample(1.0, events=2)),
        (_sample(1.0, digest="one"), _sample(1.0, digest="two")),
    ],
)
def test_summarize_samples_rejects_a_changing_target(samples: tuple[Sample, ...]) -> None:
    with pytest.raises(BenchmarkError):
        summarize_samples(samples)
