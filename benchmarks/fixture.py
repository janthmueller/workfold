"""Generate scalable local Git and filesystem benchmark fixtures."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, cast


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """Size and evidence mix for one synthetic benchmark repository."""

    commits: int
    tracked_files: int
    untracked_files: int
    ignored_files: int
    reflog_updates: int
    symlinks: int
    tracked_files_per_directory: int = 100
    filesystem_files_per_directory: int = 250

    def __post_init__(self) -> None:
        values = asdict(self)
        if self.commits < 1 or self.tracked_files < 1:
            raise ValueError("fixture commits and tracked_files must be positive")
        if any(value < 0 for value in values.values()):
            raise ValueError("fixture sizes must not be negative")
        if self.tracked_files_per_directory < 1 or self.filesystem_files_per_directory < 1:
            raise ValueError("fixture directory densities must be positive")


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    """Stable metadata describing a generated fixture."""

    root: str
    created_at: str
    commits: int
    current_week_commits: int
    tracked_files: int
    untracked_files: int
    ignored_files: int
    reflog_updates: int
    symlinks: int
    annotated_tags: int
    tracked_files_per_directory: int
    filesystem_files_per_directory: int


PRESETS: dict[str, FixtureSpec] = {
    "small": FixtureSpec(
        commits=250,
        tracked_files=50,
        untracked_files=300,
        ignored_files=100,
        reflog_updates=8,
        symlinks=4,
    ),
    "medium": FixtureSpec(
        commits=20_000,
        tracked_files=2_000,
        untracked_files=20_000,
        ignored_files=5_000,
        reflog_updates=100,
        symlinks=50,
    ),
    "directory-heavy": FixtureSpec(
        commits=5_000,
        tracked_files=5_000,
        untracked_files=5_000,
        ignored_files=1_000,
        reflog_updates=50,
        symlinks=50,
        tracked_files_per_directory=1,
        filesystem_files_per_directory=1,
    ),
    "large": FixtureSpec(
        commits=200_000,
        tracked_files=10_000,
        untracked_files=100_000,
        ignored_files=25_000,
        reflog_updates=500,
        symlinks=200,
    ),
}

_IDENTITIES = (
    ("Ada Benchmark", "ada@example.test"),
    ("Ben Benchmark", "ben@example.test"),
    ("Cleo Benchmark", "cleo@example.test"),
    ("Drew Benchmark", "drew@example.test"),
)


def create_fixture(
    root: Path,
    spec: FixtureSpec,
    *,
    now: datetime | None = None,
) -> FixtureManifest:
    """Create one non-destructive synthetic repository in an empty directory."""

    target = root.resolve()
    _prepare_empty_directory(target)
    clock = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    _run_git(target, "init", "--initial-branch=main")
    _run_git(target, "config", "core.logAllRefUpdates", "true")
    _fast_import_history(target, spec, clock)
    _run_git(target, "reset", "--hard", "refs/heads/main")
    _create_annotated_tag(target, clock)
    _create_reflog_history(target, spec.reflog_updates)
    _run_git(target, "update-ref", "refs/remotes/origin/benchmark", "HEAD")
    _write_file_set(
        target / "untracked",
        spec.untracked_files,
        files_per_directory=spec.filesystem_files_per_directory,
    )
    _write_file_set(
        target / "ignored",
        spec.ignored_files,
        files_per_directory=spec.filesystem_files_per_directory,
    )
    symlinks = _create_symlinks(
        target,
        spec.symlinks,
        spec.tracked_files,
        tracked_files_per_directory=spec.tracked_files_per_directory,
    )
    current_week_commits = spec.commits - _historical_commit_count(spec.commits)
    return FixtureManifest(
        root=os.fspath(target),
        created_at=clock.isoformat(),
        commits=spec.commits,
        current_week_commits=current_week_commits,
        tracked_files=spec.tracked_files + 1,
        untracked_files=spec.untracked_files,
        ignored_files=spec.ignored_files,
        reflog_updates=spec.reflog_updates,
        symlinks=symlinks,
        annotated_tags=1,
        tracked_files_per_directory=spec.tracked_files_per_directory,
        filesystem_files_per_directory=spec.filesystem_files_per_directory,
    )


def _prepare_empty_directory(root: Path) -> None:
    if root.exists():
        if not root.is_dir():
            raise ValueError(f"fixture target is not a directory: {root}")
        if next(root.iterdir(), None) is not None:
            raise ValueError(f"fixture target must be empty: {root}")
    else:
        root.mkdir(parents=True)


def _fast_import_history(root: Path, spec: FixtureSpec, now: datetime) -> None:
    with tempfile.TemporaryFile(mode="w+b") as errors:
        process = subprocess.Popen(
            ("git", "fast-import", "--quiet", "--date-format=raw"),
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            shell=False,
        )
        if process.stdin is None:
            raise RuntimeError("git fast-import did not expose stdin")
        stream = cast(BinaryIO, process.stdin)
        mark = 1
        try:
            for index in range(spec.commits):
                content_mark = mark
                mark += 1
                _write_blob(stream, content_mark, f"benchmark content {index}\n".encode())
                ignore_mark: int | None = None
                if index == 0:
                    ignore_mark = mark
                    mark += 1
                    _write_blob(stream, ignore_mark, b"ignored/\n")
                commit_mark = mark
                mark += 1
                _write_commit(stream, index, spec, now, content_mark, commit_mark, ignore_mark)
            stream.write(b"done\n")
            stream.close()
        except (BrokenPipeError, OSError):
            stream.close()
        return_code = process.wait()
        if return_code != 0:
            errors.seek(0)
            message = errors.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"git fast-import failed ({return_code}): {message}")


def _write_blob(stream: BinaryIO, mark: int, payload: bytes) -> None:
    stream.write(b"blob\n")
    stream.write(f"mark :{mark}\n".encode("ascii"))
    _write_data(stream, payload)


def _write_commit(
    stream: BinaryIO,
    index: int,
    spec: FixtureSpec,
    now: datetime,
    content_mark: int,
    commit_mark: int,
    ignore_mark: int | None,
) -> None:
    author_name, author_email = _IDENTITIES[index % len(_IDENTITIES)]
    committer_name, committer_email = _IDENTITIES[(index + 1) % len(_IDENTITIES)]
    author_epoch = _commit_epoch(index, spec.commits, now)
    committer_epoch = author_epoch + 30
    stream.write(b"commit refs/heads/main\n")
    stream.write(f"mark :{commit_mark}\n".encode("ascii"))
    stream.write(f"author {author_name} <{author_email}> {author_epoch} +0000\n".encode("ascii"))
    stream.write(f"committer {committer_name} <{committer_email}> {committer_epoch} +0000\n".encode("ascii"))
    _write_data(stream, f"benchmark commit {index}".encode())
    path_index = index % spec.tracked_files
    path = f"tracked/{path_index // spec.tracked_files_per_directory:04d}/file-{path_index:06d}.txt"
    stream.write(f"M 100644 :{content_mark} {path}\n".encode("ascii"))
    if ignore_mark is not None:
        stream.write(f"M 100644 :{ignore_mark} .gitignore\n".encode("ascii"))
    stream.write(b"\n")


def _write_data(stream: BinaryIO, payload: bytes) -> None:
    stream.write(f"data {len(payload)}\n".encode("ascii"))
    stream.write(payload)
    stream.write(b"\n")


def _commit_epoch(index: int, commit_count: int, now: datetime) -> int:
    historical_count = _historical_commit_count(commit_count)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if index < historical_count:
        start = datetime(2010, 1, 1, tzinfo=timezone.utc)
        end = week_start - timedelta(days=1)
        span = max(1, int((end - start).total_seconds()))
        return int(start.timestamp()) + (index + 1) * span // (historical_count + 1)
    current_index = index - historical_count
    current_count = commit_count - historical_count
    elapsed = max(current_count + 1, int((now - week_start).total_seconds()))
    return int(week_start.timestamp()) + (current_index + 1) * elapsed // (current_count + 1)


def _historical_commit_count(commit_count: int) -> int:
    return max(0, commit_count * 9 // 10)


def _create_annotated_tag(root: Path, now: datetime) -> None:
    timestamp = f"@{int(now.timestamp())} +0000"
    _run_git(
        root,
        "tag",
        "-a",
        "benchmark-tag",
        "-m",
        "benchmark tag",
        additions={
            "GIT_COMMITTER_DATE": timestamp,
            "GIT_COMMITTER_NAME": "Benchmark Tagger",
            "GIT_COMMITTER_EMAIL": "tagger@example.test",
        },
    )


def _create_reflog_history(root: Path, count: int) -> None:
    if count == 0:
        return
    revisions = _run_git(root, "rev-list", f"--max-count={count}", "--reverse", "HEAD").splitlines()
    for index, revision in enumerate(revisions):
        _run_git(
            root,
            "update-ref",
            "--create-reflog",
            "-m",
            f"benchmark update {index}",
            "refs/heads/benchmark-reflog",
            revision,
        )


def _write_file_set(root: Path, count: int, *, files_per_directory: int) -> None:
    for index in range(count):
        directory = root / f"{index // files_per_directory:04d}"
        if index % files_per_directory == 0:
            directory.mkdir(parents=True, exist_ok=True)
        (directory / f"file-{index:06d}.txt").write_text(f"filesystem benchmark {index}\n", encoding="utf-8")


def _create_symlinks(
    root: Path,
    count: int,
    tracked_files: int,
    *,
    tracked_files_per_directory: int,
) -> int:
    if count == 0:
        return 0
    links = root / "links"
    links.mkdir()
    created = 0
    for index in range(count):
        target_index = index % tracked_files
        target = (
            Path("..")
            / "tracked"
            / f"{target_index // tracked_files_per_directory:04d}"
            / f"file-{target_index:06d}.txt"
        )
        try:
            (links / f"link-{index:06d}").symlink_to(target)
        except OSError:
            break
        created += 1
    return created


def _run_git(
    root: Path,
    *arguments: str,
    additions: dict[str, str] | None = None,
) -> str:
    environment = _git_environment()
    if additions is not None:
        environment.update(additions)
    completed = subprocess.run(
        ("git", "--no-pager", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", *arguments),
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fixture Git command failed ({completed.returncode}): {completed.stderr}")
    return completed.stdout.strip()


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    return environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a synthetic Workfold benchmark repository.")
    parser.add_argument("target", type=Path, help="new or empty target directory")
    parser.add_argument("--size", choices=tuple(PRESETS), default="medium")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = create_fixture(arguments.target, PRESETS[arguments.size])
    except (OSError, RuntimeError, ValueError) as error:
        print(f"benchmark fixture: error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PRESETS", "FixtureManifest", "FixtureSpec", "create_fixture", "main"]
