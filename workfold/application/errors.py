"""Stable operational failures crossing the application/CLI boundary."""

from __future__ import annotations


class OperationalError(RuntimeError):
    """A runtime failure that should be reported without a Python traceback."""


__all__ = ["OperationalError"]
