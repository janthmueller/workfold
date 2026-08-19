"""Deadline handling shared by streaming local subprocess readers."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol


class DeadlineProcess(Protocol):
    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


@contextmanager
def streaming_deadline(
    process: DeadlineProcess,
    timeout: float | None,
) -> Generator[threading.Event, None, None]:
    """Kill *process* at the deadline and expose whether it expired."""

    expired = threading.Event()
    finished = threading.Event()
    timer: threading.Timer | None = None
    if timeout is not None:

        def expire() -> None:
            if finished.is_set() or process.poll() is not None:
                return
            expired.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
    try:
        yield expired
    finally:
        finished.set()
        if timer is not None:
            timer.cancel()


__all__ = ["DeadlineProcess", "streaming_deadline"]
