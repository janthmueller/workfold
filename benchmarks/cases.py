"""Named CLI workloads used by the benchmark runner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One stable Workfold argument combination."""

    name: str
    arguments: tuple[str, ...]
    description: str


CASES = (
    BenchmarkCase("git-current", ("-p", "git"), "low-noise Git evidence in the current week"),
    BenchmarkCase("git-all", ("-p", "git", "-t", "all"), "low-noise Git evidence across all dates"),
    BenchmarkCase("fs-current", ("-p", "fs"), "low-noise filesystem evidence in the current week"),
    BenchmarkCase("fs-all", ("-p", "fs", "-t", "all"), "low-noise filesystem evidence across all dates"),
    BenchmarkCase("both-current", ("-p", "both"), "low-noise Git and filesystem evidence in the current week"),
    BenchmarkCase(
        "both-all",
        ("-p", "both", "-t", "all"),
        "low-noise Git and filesystem evidence across all dates",
    ),
    BenchmarkCase(
        "git-portable-current",
        ("-p", "portable", "--git-commits-from", "all-refs"),
        "portable Git events from all locally stored refs in the current week",
    ),
    BenchmarkCase(
        "git-portable-all",
        ("-p", "portable", "--git-commits-from", "all-refs", "-t", "all"),
        "portable Git events from all locally stored refs across all dates",
    ),
    BenchmarkCase(
        "git-full-current",
        ("-e", "git:*", "--git-commits-from", "all-refs"),
        "every Git event kind from all locally stored refs in the current week",
    ),
    BenchmarkCase(
        "git-full-all",
        ("-e", "git:*", "--git-commits-from", "all-refs", "-t", "all"),
        "every Git event kind from all locally stored refs across all dates",
    ),
    BenchmarkCase(
        "fs-full-current",
        ("-e", "fs:*", "--include-ignored"),
        "every filesystem event kind including ignored entries in the current week",
    ),
    BenchmarkCase(
        "fs-full-all",
        ("-e", "fs:*", "--include-ignored", "-t", "all"),
        "every filesystem event kind including ignored entries across all dates",
    ),
    BenchmarkCase(
        "both-full-current",
        ("-p", "full", "--git-commits-from", "all-refs", "--include-ignored"),
        "every event kind using all local refs and including ignored entries in the current week",
    ),
    BenchmarkCase(
        "both-full-all",
        ("-p", "full", "--git-commits-from", "all-refs", "--include-ignored", "-t", "all"),
        "every event kind using all local refs and including ignored entries across all dates",
    ),
)

CASE_BY_NAME = {case.name: case for case in CASES}

SUITES: dict[str, tuple[str, ...]] = {
    "quick": ("git-current", "fs-current", "both-current"),
    "scope": ("git-current", "git-all", "fs-current", "fs-all", "both-current", "both-all"),
    "profiles": (
        "git-portable-current",
        "git-portable-all",
        "git-full-current",
        "git-full-all",
        "fs-full-current",
        "fs-full-all",
        "both-full-current",
        "both-full-all",
    ),
    "complete": tuple(case.name for case in CASES),
}


def select_cases(suite: str, names: tuple[str, ...]) -> tuple[BenchmarkCase, ...]:
    """Resolve an ordered suite or explicit repeatable case selection."""

    selected_names = names or SUITES[suite]
    return tuple(CASE_BY_NAME[name] for name in dict.fromkeys(selected_names))


__all__ = ["CASES", "CASE_BY_NAME", "SUITES", "BenchmarkCase", "select_cases"]
