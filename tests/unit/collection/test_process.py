from __future__ import annotations

import threading

from wuf.collection.process import streaming_deadline


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.killed.set()


def test_streaming_deadline_kills_a_process_and_reports_expiry() -> None:
    process = _Process()

    with streaming_deadline(process, 0.01) as expired:
        assert process.killed.wait(timeout=1)

    assert expired.is_set()


def test_streaming_deadline_cancels_when_the_operation_finishes() -> None:
    process = _Process()

    with streaming_deadline(process, 0.05) as expired:
        pass

    assert not expired.is_set()
    assert not process.killed.wait(timeout=0.1)
