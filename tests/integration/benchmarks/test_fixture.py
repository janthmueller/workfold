from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from benchmarks.fixture import FixtureSpec, create_fixture
from benchmarks.metrics import measure_command

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_synthetic_fixture_refuses_to_modify_a_nonempty_target(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="must be empty"):
        create_fixture(
            root,
            FixtureSpec(
                commits=1,
                tracked_files=1,
                untracked_files=0,
                ignored_files=0,
                reflog_updates=0,
                symlinks=0,
            ),
        )

    assert marker.read_text(encoding="utf-8") == "user data"


def test_synthetic_fixture_exercises_complete_workfold_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    manifest = create_fixture(
        root,
        FixtureSpec(
            commits=20,
            tracked_files=5,
            untracked_files=7,
            ignored_files=3,
            reflog_updates=4,
            symlinks=2,
        ),
        now=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
    )

    assert manifest.commits == 20
    assert manifest.current_week_commits == 2
    assert _git(root, "rev-list", "--count", "main") == "20"
    assert _git(root, "cat-file", "-t", "refs/tags/benchmark-tag") == "tag"
    assert len(_git(root, "reflog", "show", "--format=%H", "benchmark-reflog").splitlines()) == 4
    assert _git(root, "check-ignore", "ignored/0000/file-000000.txt") == "ignored/0000/file-000000.txt"

    environment = dict(os.environ)
    environment.update({"NO_COLOR": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    sample = measure_command(
        (
            sys.executable,
            "-m",
            "workfold",
            os.fspath(root),
            "-m",
            "both",
            "-p",
            "full",
            "-t",
            "all",
            "--no-config",
            "--no-color",
            "--strict",
            "--timezone",
            "UTC",
        ),
        cwd=PROJECT_ROOT,
        environ=environment,
        timeout_seconds=30,
        sample_interval_seconds=0.005,
    )

    assert not sample.timed_out
    assert sample.exit_code == 0, sample.stderr_excerpt
    assert sample.event_count is not None and sample.event_count > manifest.commits
    if sys.platform == "linux":
        assert sample.main_process_high_water_rss_bytes is not None
        assert sample.main_process_high_water_rss_bytes > 0
        assert sample.peak_process_tree_rss_bytes is not None
        assert sample.peak_process_tree_rss_bytes > 0


def test_synthetic_fixture_can_model_one_file_per_directory(tmp_path: Path) -> None:
    root = tmp_path / "directory-heavy"
    manifest = create_fixture(
        root,
        FixtureSpec(
            commits=3,
            tracked_files=3,
            untracked_files=3,
            ignored_files=2,
            reflog_updates=0,
            symlinks=1,
            tracked_files_per_directory=1,
            filesystem_files_per_directory=1,
        ),
        now=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
    )

    assert manifest.tracked_files_per_directory == 1
    assert manifest.filesystem_files_per_directory == 1
    assert (root / "tracked" / "0002" / "file-000002.txt").is_file()
    assert (root / "untracked" / "0002" / "file-000002.txt").is_file()
    assert (root / "ignored" / "0001" / "file-000001.txt").is_file()
    assert manifest.symlinks in {0, 1}
    if manifest.symlinks:
        assert (root / "links" / "link-000000").is_symlink()


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", os.fspath(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()
