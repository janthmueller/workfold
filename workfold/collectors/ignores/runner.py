"""Constrained local Git runner for ignore and inventory operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

from workfold.collectors.ignores.models import GitIgnoreCommandError

_GIT_SAFETY_OPTIONS: Final[tuple[str, ...]] = (
    "--no-pager",
    "-c",
    "color.ui=false",
    "-c",
    "core.pager=cat",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.allow=never",
)
_GIT_IGNORE_COMMANDS: Final[frozenset[str]] = frozenset({"check-ignore", "ls-files", "rev-parse"})

ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitIgnoreRunner:
    """Run only local Git commands needed for standard ignore evaluation."""

    def __init__(
        self,
        executable: str = "git",
        *,
        process_runner: ProcessRunner = subprocess.run,
        base_environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
        stderr_limit: int = 16_384,
    ) -> None:
        if stderr_limit < 0:
            raise ValueError("stderr_limit must be non-negative")
        self._executable = executable
        self._process_runner = process_runner
        self._base_environment = dict(os.environ if base_environment is None else base_environment)
        self._timeout = timeout
        self._stderr_limit = stderr_limit

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        allowed_returncodes: Collection[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a validated read-only Git ignore command without a shell."""

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
            raise GitIgnoreCommandError(
                code="git_not_found_for_ignores",
                message=f"Git executable was not found: {self._executable}",
                cwd=cwd,
                command=command,
                unavailable=True,
            ) from error
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            raise GitIgnoreCommandError(
                code="git_ignore_timeout",
                message="Git ignore evaluation exceeded its timeout",
                cwd=cwd,
                command=command,
                stderr=stderr[: self._stderr_limit],
            ) from error
        except OSError as error:
            raise GitIgnoreCommandError(
                code="git_ignore_spawn_error",
                message=f"Git ignore evaluation could not start: {error}",
                cwd=cwd,
                command=command,
            ) from error
        if completed.returncode not in allowed_returncodes:
            raise GitIgnoreCommandError(
                code="git_ignore_command_failed",
                message=f"Git ignore command failed with exit status {completed.returncode}",
                cwd=cwd,
                command=command,
                returncode=completed.returncode,
                stderr=completed.stderr[: self._stderr_limit],
            )
        return completed

    def consume_stdout(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        consumer: Callable[[bytes], None],
        allowed_returncodes: Collection[int] = (0,),
    ) -> bytes:
        """Feed bounded stdout chunks to *consumer* and return bounded stderr."""

        if self._process_runner is not subprocess.run or type(self).run is not GitIgnoreRunner.run or self._timeout is not None:
            completed = self.run(arguments, cwd=cwd, allowed_returncodes=allowed_returncodes)
            consumer(completed.stdout)
            return completed.stderr[: self._stderr_limit]

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
                raise GitIgnoreCommandError(
                    code="git_not_found_for_ignores",
                    message=f"Git executable was not found: {self._executable}",
                    cwd=cwd,
                    command=command,
                    unavailable=True,
                ) from error
            except OSError as error:
                raise GitIgnoreCommandError(
                    code="git_ignore_spawn_error",
                    message=f"Git ignore evaluation could not start: {error}",
                    cwd=cwd,
                    command=command,
                ) from error

            completed = False
            try:
                if process.stdout is None:
                    raise RuntimeError("streaming Git ignore process omitted stdout")
                while chunk := process.stdout.read(64 * 1024):
                    consumer(chunk)
                process.stdout.close()
                returncode = process.wait()
                completed = True
            finally:
                if not completed and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            stderr_file.seek(0)
            stderr = stderr_file.read(self._stderr_limit + 1)
            if returncode not in allowed_returncodes:
                raise GitIgnoreCommandError(
                    code="git_ignore_command_failed",
                    message=f"Git ignore command failed with exit status {returncode}",
                    cwd=cwd,
                    command=command,
                    returncode=returncode,
                    stderr=stderr[: self._stderr_limit],
                )
            return stderr[: self._stderr_limit]

    def _command(self, arguments: Sequence[str], *, cwd: Path) -> tuple[str, ...]:
        if not arguments or arguments[0] not in _GIT_IGNORE_COMMANDS:
            raise GitIgnoreCommandError(
                code="unsafe_git_ignore_command",
                message="only rev-parse, check-ignore, and ls-files are allowed for filesystem ignores",
                cwd=cwd,
                command=tuple(arguments),
            )
        if any("\0" in argument for argument in arguments):
            raise GitIgnoreCommandError(
                code="unsafe_git_ignore_argument",
                message="Git ignore command arguments cannot contain NUL bytes",
                cwd=cwd,
                command=tuple(arguments),
            )
        return (self._executable, *_GIT_SAFETY_OPTIONS, *arguments)

    def _environment(self) -> dict[str, str]:
        environment = dict(self._base_environment)
        for name in tuple(environment):
            if (
                name
                in {
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_CEILING_DIRECTORIES",
                    "GIT_COMMON_DIR",
                    "GIT_CONFIG",
                    "GIT_CONFIG_COUNT",
                    "GIT_CONFIG_PARAMETERS",
                    "GIT_DIR",
                    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                    "GIT_INDEX_FILE",
                    "GIT_NAMESPACE",
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_PREFIX",
                    "GIT_WORK_TREE",
                }
                or name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")
                or name.startswith("GIT_TRACE")
            ):
                environment.pop(name, None)
        environment.update(
            {
                "GIT_ASKPASS": "",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
                "PAGER": "cat",
            }
        )
        return environment
