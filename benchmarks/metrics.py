"""Low-overhead subprocess timing and memory sampling."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_EVENTS_PATTERN = re.compile(r"^Events\s+([0-9][0-9,]*)\s*$", re.MULTILINE)
_STDERR_EXCERPT_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Cumulative resource counters for completed child processes."""

    user_seconds: float
    system_seconds: float
    minor_page_faults: int
    major_page_faults: int
    voluntary_context_switches: int
    involuntary_context_switches: int


@dataclass(frozen=True, slots=True)
class Sample:
    """One complete end-to-end Wuf process measurement."""

    wall_seconds: float
    cpu_user_seconds: float | None
    cpu_system_seconds: float | None
    main_process_high_water_rss_bytes: int | None
    peak_process_tree_rss_bytes: int | None
    minor_page_faults: int | None
    major_page_faults: int | None
    voluntary_context_switches: int | None
    involuntary_context_switches: int | None
    exit_code: int
    timed_out: bool
    stdout_bytes: int
    stdout_lines: int
    stderr_bytes: int
    stderr_lines: int
    stdout_sha256: str
    event_count: int | None
    stderr_excerpt: str


def measure_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    timeout_seconds: float,
    sample_interval_seconds: float,
) -> Sample:
    """Run one command and measure its complete process lifetime."""

    resources_before = _resource_snapshot()
    started = time.perf_counter()
    timed_out = False
    main_high_water_rss: int | None = None
    peak_rss: int | None = None
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        process = subprocess.Popen(
            tuple(command),
            cwd=cwd,
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            shell=False,
            start_new_session=os.name == "posix",
        )
        deadline = started + timeout_seconds
        while process.poll() is None:
            memory = _linux_process_memory_bytes(process.pid)
            if memory is not None:
                tree_rss, main_high_water = memory
                peak_rss = tree_rss if peak_rss is None else max(peak_rss, tree_rss)
                main_high_water_rss = (
                    main_high_water if main_high_water_rss is None else max(main_high_water_rss, main_high_water)
                )
            if time.perf_counter() >= deadline:
                timed_out = True
                _terminate_process_tree(process)
                break
            time.sleep(sample_interval_seconds)
        exit_code = process.wait()
        finished = time.perf_counter()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()

    resources_after = _resource_snapshot()
    resource_delta = _resource_delta(resources_before, resources_after)
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    return Sample(
        wall_seconds=finished - started,
        cpu_user_seconds=resource_delta.user_seconds if resource_delta is not None else None,
        cpu_system_seconds=resource_delta.system_seconds if resource_delta is not None else None,
        main_process_high_water_rss_bytes=main_high_water_rss,
        peak_process_tree_rss_bytes=peak_rss,
        minor_page_faults=resource_delta.minor_page_faults if resource_delta is not None else None,
        major_page_faults=resource_delta.major_page_faults if resource_delta is not None else None,
        voluntary_context_switches=(resource_delta.voluntary_context_switches if resource_delta is not None else None),
        involuntary_context_switches=(
            resource_delta.involuntary_context_switches if resource_delta is not None else None
        ),
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_bytes=len(stdout),
        stdout_lines=_line_count(stdout),
        stderr_bytes=len(stderr),
        stderr_lines=_line_count(stderr),
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        event_count=parse_event_count(stdout_text),
        stderr_excerpt=stderr_text[-_STDERR_EXCERPT_LIMIT:],
    )


def parse_event_count(output: str) -> int | None:
    """Read the exact Events summary count from no-color terminal output."""

    match = _EVENTS_PATTERN.search(output)
    return int(match.group(1).replace(",", "")) if match is not None else None


def _line_count(value: bytes) -> int:
    return value.count(b"\n") + int(bool(value) and not value.endswith(b"\n"))


def _resource_snapshot() -> ResourceSnapshot | None:
    try:
        import resource
    except ImportError:  # pragma: no cover - resource is unavailable on Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ResourceSnapshot(
        user_seconds=usage.ru_utime,
        system_seconds=usage.ru_stime,
        minor_page_faults=usage.ru_minflt,
        major_page_faults=usage.ru_majflt,
        voluntary_context_switches=usage.ru_nvcsw,
        involuntary_context_switches=usage.ru_nivcsw,
    )


def _resource_delta(
    before: ResourceSnapshot | None,
    after: ResourceSnapshot | None,
) -> ResourceSnapshot | None:
    if before is None or after is None:
        return None
    return ResourceSnapshot(
        user_seconds=max(0.0, after.user_seconds - before.user_seconds),
        system_seconds=max(0.0, after.system_seconds - before.system_seconds),
        minor_page_faults=max(0, after.minor_page_faults - before.minor_page_faults),
        major_page_faults=max(0, after.major_page_faults - before.major_page_faults),
        voluntary_context_switches=max(
            0,
            after.voluntary_context_switches - before.voluntary_context_switches,
        ),
        involuntary_context_switches=max(
            0,
            after.involuntary_context_switches - before.involuntary_context_switches,
        ),
    )


def _linux_process_memory_bytes(root_pid: int) -> tuple[int, int] | None:
    """Sample Linux main-process high-water RSS and current process-tree RSS."""

    if sys.platform != "linux":
        return None
    pending = [root_pid]
    seen: set[int] = set()
    total_kib = 0
    main_high_water_kib = 0
    observed = False
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process_root = Path("/proc") / str(pid)
        try:
            status = (process_root / "status").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        observed = True
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                fields = line.split()
                if len(fields) >= 2:
                    total_kib += int(fields[1])
            elif pid == root_pid and line.startswith("VmHWM:"):
                fields = line.split()
                if len(fields) >= 2:
                    main_high_water_kib = int(fields[1])
        try:
            children = (process_root / "task" / str(pid) / "children").read_text(encoding="ascii")
        except OSError:
            children = ""
        pending.extend(int(value) for value in children.split())
    return (total_kib * 1_024, main_high_water_kib * 1_024) if observed else None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:  # pragma: no cover - exercised by Windows CI only
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:  # pragma: no cover - exercised by Windows CI only
        process.kill()


__all__ = ["Sample", "measure_command", "parse_event_count"]
