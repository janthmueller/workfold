"""Structured failures from constrained local Git commands."""

from __future__ import annotations

from pathlib import Path

from workfold.collection.diagnostics import DiagnosticCategory


class GitCommandError(RuntimeError):
    """Describe a Git subprocess failure without losing byte-exact stderr."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        command: tuple[str, ...],
        cwd: Path,
        returncode: int | None = None,
        stderr: bytes = b"",
        stderr_truncated: bool = False,
        hint: str | None = None,
        category: DiagnosticCategory = DiagnosticCategory.COLLECTION,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.command = command
        self.cwd = cwd
        self.returncode = returncode
        self.stderr = stderr
        self.stderr_truncated = stderr_truncated
        self.hint = hint
        self.category = category

    @property
    def stderr_text(self) -> str:
        """Decode diagnostic stderr without discarding invalid bytes."""

        text = self.stderr.decode("utf-8", errors="surrogateescape").rstrip()
        return f"{text}…" if self.stderr_truncated else text


__all__ = ["GitCommandError"]
