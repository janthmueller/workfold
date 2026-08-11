"""Constrained, non-interactive local Git command execution."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic
from workfold.collectors.process_timeout import streaming_deadline

LOCAL_READ_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "cat-file",
        "check-ignore",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "reflog",
        "rev-list",
        "rev-parse",
        "show-ref",
    }
)
REPOSITORY_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
REPOSITORY_ENVIRONMENT_PREFIXES: Final[tuple[str, ...]] = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)


class GitCommandError(RuntimeError):
    """Structured subprocess failure raised by :class:`GitRunner`."""

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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.command = command
        self.cwd = cwd
        self.returncode = returncode
        self.stderr = stderr
        self.stderr_truncated = stderr_truncated
        self.hint = hint

    @property
    def stderr_text(self) -> str:
        """Decode diagnostic stderr without discarding invalid bytes."""

        text = self.stderr.decode("utf-8", errors="surrogateescape").rstrip()
        return f"{text}…" if self.stderr_truncated else text


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitRunner:
    """Invoke an allow-listed set of read-only local Git plumbing commands."""

    def __init__(
        self,
        executable: str = "git",
        *,
        process_runner: ProcessRunner = subprocess.run,
        stderr_limit: int = 16_384,
        timeout: float | None = None,
        base_environment: Mapping[str, str] | None = None,
        stream_output: bool | None = None,
    ) -> None:
        if stderr_limit < 0:
            raise ValueError("stderr_limit must be non-negative")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        self._executable = executable
        self._process_runner = process_runner
        self._stderr_limit = stderr_limit
        self._timeout = timeout
        self._base_environment = dict(os.environ if base_environment is None else base_environment)
        self._stream_output = process_runner is subprocess.run if stream_output is None else stream_output
        if self._stream_output and process_runner is not subprocess.run:
            raise ValueError("stream_output requires the default subprocess runner")

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        allowed_returncodes: Collection[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one local command with prompts, pagers, protocols and lazy fetch disabled."""

        command = self._command(arguments, cwd=cwd)
        try:
            completed = self._process_runner(
                command,
                cwd=os.fspath(cwd),
                env=self._environment(),
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=self._timeout,
            )
        except FileNotFoundError as error:
            raise GitCommandError(
                code="git_not_found",
                message=f"Git executable was not found: {self._executable}",
                command=command,
                cwd=cwd,
                hint="Install Git or use --mode fs.",
            ) from error
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            bounded, truncated = self._bounded_stderr(stderr)
            raise GitCommandError(
                code="git_command_timeout",
                message="Git command exceeded the configured timeout",
                command=command,
                cwd=cwd,
                stderr=bounded,
                stderr_truncated=truncated,
            ) from error
        except OSError as error:
            raise GitCommandError(
                code="git_spawn_error",
                message=f"Git command could not be started: {error}",
                command=command,
                cwd=cwd,
            ) from error
        if completed.returncode not in allowed_returncodes:
            bounded, truncated = self._bounded_stderr(completed.stderr)
            raise GitCommandError(
                code="git_command_failed",
                message=f"Git command failed with exit status {completed.returncode}",
                command=command,
                cwd=cwd,
                returncode=completed.returncode,
                stderr=bounded,
                stderr_truncated=truncated,
            )
        return completed

    def iter_stdout_lines(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        allowed_returncodes: Collection[int] = (0,),
    ) -> Iterator[bytes]:
        """Yield stdout records without retaining an unbounded Git response."""

        if not self._stream_output:
            yield from self.run(arguments, cwd=cwd, allowed_returncodes=allowed_returncodes).stdout.splitlines(
                keepends=True
            )
            return

        command = self._command(arguments, cwd=cwd)
        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=os.fspath(cwd),
                    env=self._environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    shell=False,
                )
            except FileNotFoundError as error:
                raise GitCommandError(
                    code="git_not_found",
                    message=f"Git executable was not found: {self._executable}",
                    command=command,
                    cwd=cwd,
                    hint="Install Git or use --mode fs.",
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
                with streaming_deadline(process, self._timeout) as expired:
                    if process.stdout is None:
                        raise RuntimeError("streaming Git process omitted stdout")
                    yield from process.stdout
                    process.stdout.close()
                    returncode = process.wait()
                    if expired.is_set():
                        stderr_file.seek(0)
                        stderr = stderr_file.read(self._stderr_limit + 1)
                        bounded, truncated = self._bounded_stderr(stderr)
                        raise GitCommandError(
                            code="git_command_timeout",
                            message="Git command exceeded the configured timeout",
                            command=command,
                            cwd=cwd,
                            stderr=bounded,
                            stderr_truncated=truncated,
                        )
                    completed = True
            finally:
                if not completed and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            if returncode not in allowed_returncodes:
                stderr_file.seek(0)
                stderr = stderr_file.read(self._stderr_limit + 1)
                bounded, truncated = self._bounded_stderr(stderr)
                raise GitCommandError(
                    code="git_command_failed",
                    message=f"Git command failed with exit status {returncode}",
                    command=command,
                    cwd=cwd,
                    returncode=returncode,
                    stderr=bounded,
                    stderr_truncated=truncated,
                )

    def _environment(self) -> dict[str, str]:
        environment = dict(self._base_environment)
        for name in tuple(environment):
            if name in REPOSITORY_ENVIRONMENT or name.startswith(REPOSITORY_ENVIRONMENT_PREFIXES):
                environment.pop(name)
        environment.update(
            {
                "GIT_ASKPASS": "",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_EXTERNAL_DIFF": "",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
                "PAGER": "cat",
            }
        )
        return environment

    def _command(self, arguments: Sequence[str], *, cwd: Path) -> tuple[str, ...]:
        if not arguments or arguments[0] not in LOCAL_READ_COMMANDS:
            command_name = arguments[0] if arguments else "<empty>"
            raise GitCommandError(
                code="unsafe_git_command",
                message=f"Git command is not allowed for local collection: {command_name}",
                command=tuple(arguments),
                cwd=cwd,
                hint="Workfold only invokes allow-listed, read-only Git commands.",
            )
        if any("\0" in argument for argument in arguments):
            raise GitCommandError(
                code="unsafe_git_argument",
                message="Git command argument contains a NUL byte",
                command=tuple(arguments),
                cwd=cwd,
            )
        return (
            self._executable,
            "--no-pager",
            "-c",
            "color.ui=false",
            "-c",
            "core.pager=cat",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            *arguments,
        )

    def _bounded_stderr(self, stderr: bytes) -> tuple[bytes, bool]:
        if len(stderr) <= self._stderr_limit:
            return stderr, False
        return stderr[: self._stderr_limit], True


def command_diagnostic(error: GitCommandError, *, stage: str, target: Path) -> CollectorDiagnostic:
    details = error.stderr_text
    message = str(error) if not details else f"{error}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage=stage,
        target=os.fspath(target),
        path=os.fspath(target),
        message=message,
        hint=error.hint,
    )
