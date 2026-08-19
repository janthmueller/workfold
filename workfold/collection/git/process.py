"""Lifecycle management for bounded-memory Git stdout streams."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

from workfold.collection.diagnostics import DiagnosticCategory
from workfold.collection.git.command_error import GitCommandError
from workfold.collection.process import streaming_deadline


class _TrackedBinaryOutput:
    """Track whether a streaming consumer has conclusively observed EOF."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.eof_observed = False

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if not data or size < 0 or len(data) < size:
            self.eof_observed = True
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._stream.readline(size)
        if not data or (not data.endswith(b"\n") and (size < 0 or len(data) < size)):
            self.eof_observed = True
        return data

    def __iter__(self) -> _TrackedBinaryOutput:
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


@contextmanager
def open_git_stdout(
    *,
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    input_stream: BinaryIO | None,
    allowed_returncodes: Collection[int],
    timeout: float | None,
    stderr_limit: int,
) -> Generator[BinaryIO, None, None]:
    """Yield stdout and reconcile consumer, timeout, and process failures."""

    try:
        stderr_file = cast(BinaryIO, tempfile.TemporaryFile())
    except OSError as error:
        raise GitCommandError(
            code="git_stream_setup_error",
            message=f"Git command diagnostics could not be buffered: {error}",
            command=command,
            cwd=cwd,
            hint="Check that the system temporary directory is available and writable.",
        ) from error

    with stderr_file:
        try:
            process = subprocess.Popen(
                command,
                cwd=os.fspath(cwd),
                env=environment,
                stdin=subprocess.DEVNULL if input_stream is None else input_stream,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                shell=False,
            )
        except FileNotFoundError as error:
            raise GitCommandError(
                code="git_not_found",
                message=f"Git executable was not found: {command[0]}",
                command=command,
                cwd=cwd,
                hint="Install Git or select filesystem events with --profile fs.",
                category=DiagnosticCategory.INVOCATION,
            ) from error
        except OSError as error:
            raise GitCommandError(
                code="git_spawn_error",
                message=f"Git command could not be started: {error}",
                command=command,
                cwd=cwd,
            ) from error

        completed = False
        try:
            with streaming_deadline(process, timeout) as expired:
                if process.stdout is None:
                    raise RuntimeError("streaming Git process omitted stdout")
                stdout = _TrackedBinaryOutput(cast(BinaryIO, process.stdout))
                try:
                    yield cast(BinaryIO, stdout)
                except BaseException as error:
                    if expired.is_set():
                        process.stdout.close()
                        process.wait()
                        completed = True
                        raise _stream_timeout_error(
                            command=command,
                            cwd=cwd,
                            stderr_file=stderr_file,
                            stderr_limit=stderr_limit,
                        ) from error
                    if not stdout.eof_observed:
                        # The consumer rejected a record before reading the
                        # complete stream. Preserve that error: terminating Git
                        # during cleanup is not a subprocess failure.
                        returncode = process.poll()
                        if returncode is None:
                            raise
                        completed = True
                        if expired.is_set():
                            raise _stream_timeout_error(
                                command=command,
                                cwd=cwd,
                                stderr_file=stderr_file,
                                stderr_limit=stderr_limit,
                            ) from error
                        if returncode not in allowed_returncodes:
                            raise _stream_command_error(
                                command=command,
                                cwd=cwd,
                                returncode=returncode,
                                stderr_file=stderr_file,
                                stderr_limit=stderr_limit,
                            ) from error
                        raise
                    # Once EOF has been observed, Git cannot produce more
                    # stdout. Waiting lets a delayed non-zero exit supersede a
                    # downstream EOF or parse error deterministically.
                    returncode = process.wait()
                    completed = True
                    if expired.is_set():
                        raise _stream_timeout_error(
                            command=command,
                            cwd=cwd,
                            stderr_file=stderr_file,
                            stderr_limit=stderr_limit,
                        ) from error
                    if returncode not in allowed_returncodes:
                        raise _stream_command_error(
                            command=command,
                            cwd=cwd,
                            returncode=returncode,
                            stderr_file=stderr_file,
                            stderr_limit=stderr_limit,
                        ) from error
                    raise
                else:
                    process.stdout.close()
                    returncode = process.wait()
                    completed = True
                    if expired.is_set():
                        raise _stream_timeout_error(
                            command=command,
                            cwd=cwd,
                            stderr_file=stderr_file,
                            stderr_limit=stderr_limit,
                        )
        finally:
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
            if not completed and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        if returncode not in allowed_returncodes:
            raise _stream_command_error(
                command=command,
                cwd=cwd,
                returncode=returncode,
                stderr_file=stderr_file,
                stderr_limit=stderr_limit,
            )


def _stream_command_error(
    *,
    command: tuple[str, ...],
    cwd: Path,
    returncode: int,
    stderr_file: BinaryIO,
    stderr_limit: int,
) -> GitCommandError:
    stderr, truncated = _read_bounded_stderr(stderr_file, stderr_limit)
    return GitCommandError(
        code="git_command_failed",
        message=f"Git command failed with exit status {returncode}",
        command=command,
        cwd=cwd,
        returncode=returncode,
        stderr=stderr,
        stderr_truncated=truncated,
    )


def _stream_timeout_error(
    *,
    command: tuple[str, ...],
    cwd: Path,
    stderr_file: BinaryIO,
    stderr_limit: int,
) -> GitCommandError:
    stderr, truncated = _read_bounded_stderr(stderr_file, stderr_limit)
    return GitCommandError(
        code="git_command_timeout",
        message="Git command exceeded the configured timeout",
        command=command,
        cwd=cwd,
        stderr=stderr,
        stderr_truncated=truncated,
    )


def _read_bounded_stderr(stderr_file: BinaryIO, limit: int) -> tuple[bytes, bool]:
    stderr_file.seek(0)
    stderr = stderr_file.read(limit + 1)
    return (stderr, False) if len(stderr) <= limit else (stderr[:limit], True)


__all__ = ["open_git_stdout"]
