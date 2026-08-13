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
    BenchmarkCase("git-current", ("-m", "git"), "standard Git evidence in the current week"),
    BenchmarkCase("git-all", ("-m", "git", "-t", "all"), "standard Git evidence across all dates"),
    BenchmarkCase("fs-current", ("-m", "fs"), "standard filesystem evidence in the current week"),
    BenchmarkCase("fs-all", ("-m", "fs", "-t", "all"), "standard filesystem evidence across all dates"),
    BenchmarkCase("both-current", ("-m", "both"), "standard Git and filesystem evidence in the current week"),
    BenchmarkCase(
        "both-all",
        ("-m", "both", "-t", "all"),
        "standard Git and filesystem evidence across all dates",
    ),
    BenchmarkCase(
        "git-portable-current",
        ("-m", "git", "-p", "portable"),
        "portable Git evidence in the current week",
    ),
    BenchmarkCase(
        "git-portable-all",
        ("-m", "git", "-p", "portable", "-t", "all"),
        "portable Git evidence across all dates",
    ),
    BenchmarkCase("git-full-current", ("-m", "git", "-p", "full"), "full Git evidence in the current week"),
    BenchmarkCase(
        "git-full-all",
        ("-m", "git", "-p", "full", "-t", "all"),
        "full Git evidence across all dates",
    ),
    BenchmarkCase("fs-full-current", ("-m", "fs", "-p", "full"), "full filesystem evidence in the current week"),
    BenchmarkCase(
        "fs-full-all",
        ("-m", "fs", "-p", "full", "-t", "all"),
        "full filesystem evidence across all dates",
    ),
    BenchmarkCase(
        "both-full-current",
        ("-m", "both", "-p", "full"),
        "full Git and filesystem evidence in the current week",
    ),
    BenchmarkCase(
        "both-full-all",
        ("-m", "both", "-p", "full", "-t", "all"),
        "full Git and filesystem evidence across all dates",
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
