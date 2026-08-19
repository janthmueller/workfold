from __future__ import annotations

from io import StringIO

from wuf.cli.runner import _TransientStatus  # pyright: ignore[reportPrivateUsage]


class _TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_transient_status_is_erased_before_durable_output() -> None:
    stream = _TTYBuffer()
    status = _TransientStatus(stream, enabled=True)

    status.show("Collecting requested timestamps…")
    status.clear()
    stream.write("Time band\n")

    message = "Collecting requested timestamps…"
    assert stream.getvalue() == f"{message}\r{' ' * len(message)}\rTime band\n"


def test_transient_status_stays_silent_for_redirected_streams() -> None:
    stream = StringIO()
    status = _TransientStatus(stream, enabled=False)

    status.show("Collecting requested timestamps…")
    status.clear()

    assert stream.getvalue() == ""
