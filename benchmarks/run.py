"""Run repeatable end-to-end Workfold timing and memory benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from workfold import __version__

from benchmarks.cases import CASE_BY_NAME, SUITES, BenchmarkCase, select_cases
from benchmarks.fixture import PRESETS, FixtureManifest, create_fixture
from benchmarks.metrics import Sample, measure_command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """Comparable aggregate values for one benchmark case."""

    repetitions: int
    wall_median_seconds: float
    wall_min_seconds: float
    wall_max_seconds: float
    cpu_median_seconds: float | None
    main_rss_median_bytes: int | None
    main_rss_max_bytes: int | None
    peak_rss_median_bytes: int | None
    peak_rss_max_bytes: int | None
    event_count: int
    stdout_bytes_median: int
    stderr_bytes_max: int


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Raw and summarized measurements for one named workload."""

    name: str
    description: str
    arguments: tuple[str, ...]
    command: tuple[str, ...]
    samples: tuple[Sample, ...]
    summary: SampleSummary


class BenchmarkError(RuntimeError):
    """Raised when a benchmark run cannot produce comparable measurements."""


def summarize_samples(samples: Sequence[Sample]) -> SampleSummary:
    """Validate repeated output and summarize its central and worst-case values."""

    if not samples:
        raise ValueError("at least one benchmark sample is required")
    event_counts = {sample.event_count for sample in samples}
    if None in event_counts:
        raise BenchmarkError("Workfold output did not contain an Events summary")
    concrete_event_counts = {value for value in event_counts if value is not None}
    if len(concrete_event_counts) != 1:
        raise BenchmarkError(f"event count changed across repetitions: {sorted(concrete_event_counts)}")
    output_hashes = {sample.stdout_sha256 for sample in samples}
    if len(output_hashes) != 1:
        raise BenchmarkError("Workfold output changed across repetitions; the target was not stable")
    wall_values = [sample.wall_seconds for sample in samples]
    cpu_values = [
        sample.cpu_user_seconds + sample.cpu_system_seconds
        for sample in samples
        if sample.cpu_user_seconds is not None and sample.cpu_system_seconds is not None
    ]
    main_memory_values = [
        sample.main_process_high_water_rss_bytes
        for sample in samples
        if sample.main_process_high_water_rss_bytes is not None
    ]
    memory_values = [
        sample.peak_process_tree_rss_bytes for sample in samples if sample.peak_process_tree_rss_bytes is not None
    ]
    stdout_sizes = [sample.stdout_bytes for sample in samples]
    return SampleSummary(
        repetitions=len(samples),
        wall_median_seconds=statistics.median(wall_values),
        wall_min_seconds=min(wall_values),
        wall_max_seconds=max(wall_values),
        cpu_median_seconds=statistics.median(cpu_values) if len(cpu_values) == len(samples) else None,
        main_rss_median_bytes=(
            int(statistics.median(main_memory_values)) if len(main_memory_values) == len(samples) else None
        ),
        main_rss_max_bytes=max(main_memory_values) if len(main_memory_values) == len(samples) else None,
        peak_rss_median_bytes=int(statistics.median(memory_values)) if len(memory_values) == len(samples) else None,
        peak_rss_max_bytes=max(memory_values) if len(memory_values) == len(samples) else None,
        event_count=next(iter(concrete_event_counts)),
        stdout_bytes_median=int(statistics.median(stdout_sizes)),
        stderr_bytes_max=max(sample.stderr_bytes for sample in samples),
    )


def benchmark_case(
    case: BenchmarkCase,
    target: Path,
    *,
    command_prefix: tuple[str, ...],
    repetitions: int,
    warmups: int,
    timeout_seconds: float,
    sample_interval_seconds: float,
    environ: Mapping[str, str],
) -> CaseResult:
    """Measure one case after optional unrecorded warmup runs."""

    command = (
        *command_prefix,
        os.fspath(target),
        *case.arguments,
        "--no-config",
        "--no-color",
        "--strict",
        "--timezone",
        "UTC",
    )
    for index in range(warmups):
        print(f"[{case.name}] warmup {index + 1}/{warmups}", file=sys.stderr, flush=True)
        sample = _measure(command, timeout_seconds, sample_interval_seconds, environ)
        _validate_sample(case, sample)

    samples: list[Sample] = []
    for index in range(repetitions):
        print(f"[{case.name}] sample {index + 1}/{repetitions}", file=sys.stderr, flush=True)
        sample = _measure(command, timeout_seconds, sample_interval_seconds, environ)
        _validate_sample(case, sample)
        samples.append(sample)
        print(
            f"  {_format_seconds(sample.wall_seconds)} wall · "
            f"{_format_bytes(sample.main_process_high_water_rss_bytes)} main / "
            f"{_format_bytes(sample.peak_process_tree_rss_bytes)} tree RSS · "
            f"{sample.event_count:,} events",
            file=sys.stderr,
            flush=True,
        )
    frozen_samples = tuple(samples)
    return CaseResult(
        name=case.name,
        description=case.description,
        arguments=case.arguments,
        command=command,
        samples=frozen_samples,
        summary=summarize_samples(frozen_samples),
    )


def _measure(
    command: tuple[str, ...],
    timeout_seconds: float,
    sample_interval_seconds: float,
    environ: Mapping[str, str],
) -> Sample:
    return measure_command(
        command,
        cwd=PROJECT_ROOT,
        environ=environ,
        timeout_seconds=timeout_seconds,
        sample_interval_seconds=sample_interval_seconds,
    )


def _validate_sample(case: BenchmarkCase, sample: Sample) -> None:
    if sample.timed_out:
        raise BenchmarkError(f"{case.name} exceeded its timeout")
    if sample.exit_code != 0:
        detail = sample.stderr_excerpt.strip() or "no stderr"
        raise BenchmarkError(f"{case.name} exited with {sample.exit_code}: {detail}")
    if sample.event_count is None:
        raise BenchmarkError(f"{case.name} produced no Events summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark complete Workfold CLI processes with stable no-color output.",
        epilog=(
            "Memory includes Workfold's high-water RSS and sampled summed process-tree RSS on Linux. "
            "Use warmups for comparable warm-cache results; the runner never drops OS caches."
        ),
    )
    parser.add_argument("target", type=Path, nargs="?", help="Git repository or filesystem root")
    parser.add_argument(
        "--fixture",
        choices=tuple(PRESETS),
        help="generate and benchmark a temporary synthetic fixture instead of TARGET",
    )
    parser.add_argument("--suite", choices=tuple(SUITES), default="scope", help="built-in case set (default: scope)")
    parser.add_argument(
        "--case",
        choices=tuple(CASE_BY_NAME),
        action="append",
        default=[],
        help="benchmark one named case; repeat to override --suite",
    )
    parser.add_argument("--repetitions", type=int, default=3, help="recorded runs per case (default: 3)")
    parser.add_argument("--warmups", type=int, default=1, help="unrecorded warmup runs per case (default: 1)")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds allowed per process (default: 900)")
    parser.add_argument(
        "--sample-ms",
        type=float,
        default=5.0,
        help="Linux RSS sampling interval in milliseconds (default: 5)",
    )
    parser.add_argument("--json", type=Path, help="write raw samples and metadata to this JSON file")
    parser.add_argument(
        "--workfold-executable",
        type=Path,
        help="benchmark this installed executable instead of the checkout through python -m workfold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        _validate_arguments(arguments)
        cases = select_cases(arguments.suite, tuple(arguments.case))
        command_prefix = _command_prefix(arguments.workfold_executable)
        if arguments.fixture is not None:
            with tempfile.TemporaryDirectory(prefix="workfold-benchmark-") as temporary:
                root = Path(temporary) / "repository"
                print(f"Generating {arguments.fixture} fixture at {root} ...", file=sys.stderr, flush=True)
                started = time.perf_counter()
                fixture = create_fixture(root, PRESETS[arguments.fixture])
                print(f"Fixture ready in {_format_seconds(time.perf_counter() - started)}", file=sys.stderr, flush=True)
                return _run_benchmarks(root, fixture, cases, command_prefix, arguments)
        if arguments.target is None:
            raise BenchmarkError("TARGET is required unless --fixture is used")
        target = arguments.target.expanduser().resolve(strict=True)
        return _run_benchmarks(target, None, cases, command_prefix, arguments)
    except (BenchmarkError, OSError, ValueError) as error:
        print(f"benchmark: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("benchmark: interrupted", file=sys.stderr)
        return 130


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.fixture is not None and arguments.target is not None:
        raise BenchmarkError("TARGET and --fixture cannot be combined")
    if arguments.repetitions < 1:
        raise BenchmarkError("--repetitions must be at least 1")
    if arguments.warmups < 0:
        raise BenchmarkError("--warmups must not be negative")
    if arguments.timeout <= 0:
        raise BenchmarkError("--timeout must be positive")
    if arguments.sample_ms <= 0:
        raise BenchmarkError("--sample-ms must be positive")


def _command_prefix(executable: Path | None) -> tuple[str, ...]:
    if executable is None:
        return (sys.executable, "-m", "workfold")
    return (os.fspath(executable.expanduser().resolve(strict=True)),)


def _run_benchmarks(
    target: Path,
    fixture: FixtureManifest | None,
    cases: tuple[BenchmarkCase, ...],
    command_prefix: tuple[str, ...],
    arguments: argparse.Namespace,
) -> int:
    environment = _benchmark_environment()
    results = tuple(
        benchmark_case(
            case,
            target,
            command_prefix=command_prefix,
            repetitions=arguments.repetitions,
            warmups=arguments.warmups,
            timeout_seconds=arguments.timeout,
            sample_interval_seconds=arguments.sample_ms / 1_000,
            environ=environment,
        )
        for case in cases
    )
    print(_result_table(results))
    document = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": _host_metadata(),
        "tool": {
            "workfold_version": __version__,
            "command_prefix": command_prefix,
            "git_version": _command_output(("git", "--version")),
            "checkout_head": _command_output(("git", "-C", os.fspath(PROJECT_ROOT), "rev-parse", "HEAD")),
            "checkout_dirty": _repository_dirty(PROJECT_ROOT, include_untracked=True),
        },
        "target": _target_metadata(target),
        "fixture": asdict(fixture) if fixture is not None else None,
        "settings": {
            "suite": arguments.suite,
            "explicit_cases": tuple(arguments.case),
            "repetitions": arguments.repetitions,
            "warmups": arguments.warmups,
            "timeout_seconds": arguments.timeout,
            "rss_sample_interval_ms": arguments.sample_ms,
            "cache_policy": "OS caches left intact; optional unrecorded warmups",
            "git_config_policy": "global and system Git configuration disabled",
        },
        "cases": tuple(asdict(result) for result in results),
    }
    if arguments.json is not None:
        destination = arguments.json.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nRaw results: {destination}")
    return 0


def _benchmark_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "COLUMNS": "80",
            "LINES": "24",
            "NO_COLOR": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    return environment


def _result_table(results: Sequence[CaseResult]) -> str:
    headers = ("Case", "Wall median", "Wall range", "CPU median", "Main RSS", "Tree RSS", "Events")
    rows = [
        (
            result.name,
            _format_seconds(result.summary.wall_median_seconds),
            f"{_format_seconds(result.summary.wall_min_seconds)}–{_format_seconds(result.summary.wall_max_seconds)}",
            _format_seconds(result.summary.cpu_median_seconds),
            _format_bytes(result.summary.main_rss_max_bytes),
            _format_bytes(result.summary.peak_rss_max_bytes),
            f"{result.summary.event_count:,}",
        )
        for result in results
    ]
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return f"{value * 1_000:.0f}ms"
    if value < 10:
        return f"{value:.2f}s"
    return f"{value:.1f}s"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1_024 or suffix == "TiB":
            return f"{amount:.1f}{suffix}"
        amount /= 1_024
    raise AssertionError("unreachable byte unit")


def _host_metadata() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "cpu_count": os.cpu_count(),
        "cpu_model": _linux_cpu_model(),
        "total_memory_bytes": _linux_total_memory_bytes(),
        "load_average": _load_average(),
    }


def _target_metadata(target: Path) -> dict[str, object]:
    metadata: dict[str, object] = {"path": os.fspath(target), "is_directory": target.is_dir()}
    repository = _command_output(("git", "-C", os.fspath(target), "rev-parse", "--show-toplevel"))
    if repository is None:
        metadata["git_repository"] = False
        return metadata
    metadata.update(
        {
            "git_repository": True,
            "repository_root": repository,
            "head": _command_output(("git", "-C", os.fspath(target), "rev-parse", "HEAD")),
            "tracked_worktree_dirty": _repository_dirty(target, include_untracked=False),
            "commits_all_refs": _optional_int(
                _command_output(("git", "-C", os.fspath(target), "rev-list", "--count", "--all"))
            ),
            "git_object_store": _key_value_output(
                _command_output(("git", "-C", os.fspath(target), "count-objects", "-v"))
            ),
        }
    )
    return metadata


def _command_output(command: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            tuple(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _repository_dirty(root: Path, *, include_untracked: bool) -> bool | None:
    untracked = "all" if include_untracked else "no"
    output = _command_output(
        ("git", "-C", os.fspath(root), "status", "--porcelain=v1", f"--untracked-files={untracked}")
    )
    return bool(output) if output is not None else None


def _key_value_output(value: str | None) -> dict[str, int | str] | None:
    if value is None:
        return None
    fields: dict[str, int | str] = {}
    for line in value.splitlines():
        key, separator, raw = line.partition(": ")
        if not separator:
            continue
        fields[key] = int(raw) if raw.isdecimal() else raw
    return fields


def _linux_cpu_model() -> str | None:
    if sys.platform != "linux":
        return platform.processor() or None
    try:
        contents = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in contents.splitlines():
        if line.startswith("model name"):
            return line.partition(":")[2].strip() or None
    return None


def _linux_total_memory_bytes() -> int | None:
    if sys.platform != "linux":
        return None
    try:
        contents = Path("/proc/meminfo").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in contents.splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            return int(fields[1]) * 1_024 if len(fields) >= 2 else None
    return None


def _load_average() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (AttributeError, OSError):  # pragma: no cover - unavailable on Windows
        return None


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkError",
    "CaseResult",
    "SampleSummary",
    "benchmark_case",
    "main",
    "summarize_samples",
]
