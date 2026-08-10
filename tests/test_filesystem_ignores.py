from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from workfold.collectors.ignores import (
    ExclusionPatternError,
    ExplicitExcluder,
    GitFilesystemInventory,
    GitIgnoreCommandError,
    GitIgnoreRepository,
    GitIgnoreRunner,
    GitIgnoreService,
    IgnoreCandidate,
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_git_admin_name,
    is_nested_repository_boundary,
    looks_like_bare_repository,
)

from support.git_repo import GitRepo


class QueueRunner(GitIgnoreRunner):
    def __init__(self, outcomes: Sequence[subprocess.CompletedProcess[bytes] | GitIgnoreCommandError]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls: list[tuple[tuple[str, ...], Path, bytes | None, Collection[int]]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        allowed_returncodes: Collection[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(arguments), cwd, input_data, allowed_returncodes))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, GitIgnoreCommandError):
            raise outcome
        return outcome


def completed(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(("git",), returncode, stdout=stdout, stderr=stderr)


def test_explicit_exclusions_use_non_negating_gitwildmatch() -> None:
    excluder = ExplicitExcluder.compile(("node_modules", "*.log", "/build/"))

    assert excluder.patterns == ("node_modules", "*.log", "/build/")
    assert excluder.matches(PurePosixPath("node_modules/pkg/data"), is_directory=False)
    assert excluder.matches("nested/debug.log", is_directory=False)
    assert excluder.matches("build", is_directory=True)
    assert not excluder.matches("nested/build", is_directory=True)
    assert not excluder.matches(".", is_directory=True)
    assert not excluder.matches("/", is_directory=True)


@pytest.mark.parametrize("pattern", ["", "!keep.log", "bad\0pattern"])
def test_explicit_exclusions_reject_ambiguous_patterns(pattern: str) -> None:
    with pytest.raises(ExclusionPatternError):
        ExplicitExcluder.compile((pattern,))


def test_ignore_runner_is_local_noninteractive_and_retains_global_config(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def process(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(arguments), kwargs))
        return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"")

    runner = GitIgnoreRunner(
        process_runner=process,
        base_environment={
            "PATH": "/fixture/bin",
            "GIT_CONFIG_GLOBAL": "/fixture/global.gitconfig",
            "GIT_DIR": "/unrelated",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.bad",
            "GIT_CONFIG_VALUE_0": "!bad",
            "GIT_TRACE_SETUP": "1",
        },
    )
    result = runner.run(
        ("check-ignore", "--stdin", "-z"),
        cwd=tmp_path,
        input_data=b"one\0",
        allowed_returncodes=(0, 1),
    )

    assert result.returncode == 1
    command, options = calls[0]
    assert command[0] == "git"
    assert "protocol.allow=never" in command
    assert options["shell"] is False
    assert options["check"] is False
    assert options["input"] == b"one\0"
    assert options["env"]["GIT_CONFIG_GLOBAL"] == "/fixture/global.gitconfig"
    assert options["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert options["env"]["LC_ALL"] == "C"
    assert "GIT_DIR" not in options["env"]
    assert "GIT_CONFIG_COUNT" not in options["env"]
    assert "GIT_CONFIG_KEY_0" not in options["env"]
    assert "GIT_CONFIG_VALUE_0" not in options["env"]
    assert "GIT_TRACE_SETUP" not in options["env"]


def test_ignore_runner_rejects_unsafe_commands_before_spawn(tmp_path: Path) -> None:
    called = False

    def process(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        raise AssertionError("must not spawn")

    runner = GitIgnoreRunner(process_runner=process)
    with pytest.raises(GitIgnoreCommandError) as empty:
        runner.run((), cwd=tmp_path)
    assert empty.value.code == "unsafe_git_ignore_command"
    with pytest.raises(GitIgnoreCommandError) as command:
        runner.run(("fetch",), cwd=tmp_path)
    assert command.value.code == "unsafe_git_ignore_command"
    with pytest.raises(GitIgnoreCommandError) as nul:
        runner.run(("rev-parse", "bad\0argument"), cwd=tmp_path)
    assert nul.value.code == "unsafe_git_ignore_argument"
    assert not called


def test_ignore_runner_structures_process_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        GitIgnoreRunner(stderr_limit=-1)

    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    with pytest.raises(GitIgnoreCommandError) as not_found:
        GitIgnoreRunner(process_runner=missing).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert not_found.value.code == "git_not_found_for_ignores"
    assert not_found.value.unavailable

    def timeout_bytes(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("git", 1, stderr=b"too much detail")

    with pytest.raises(GitIgnoreCommandError) as timed_out:
        GitIgnoreRunner(process_runner=timeout_bytes, stderr_limit=4).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert timed_out.value.code == "git_ignore_timeout"
    assert timed_out.value.stderr == b"too "

    def timeout_text(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("git", 1, stderr="text")

    with pytest.raises(GitIgnoreCommandError) as text_timeout:
        GitIgnoreRunner(process_runner=timeout_text).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert text_timeout.value.stderr == b""

    def spawn_error(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("cannot spawn")

    with pytest.raises(GitIgnoreCommandError) as os_error:
        GitIgnoreRunner(process_runner=spawn_error).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert os_error.value.code == "git_ignore_spawn_error"

    def failed(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 128, stdout=b"", stderr=b"abcdefgh")

    with pytest.raises(GitIgnoreCommandError) as failure:
        GitIgnoreRunner(process_runner=failed, stderr_limit=4).run(("rev-parse", "--git-dir"), cwd=tmp_path)
    assert failure.value.code == "git_ignore_command_failed"
    assert failure.value.stderr == b"abcd"
    assert failure.value.stderr_text == "abcd"


def test_standard_git_ignore_semantics_include_nested_info_global_and_tracked_rules(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    (repo.path / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (repo.path / "nested").mkdir()
    (repo.path / "nested" / ".gitignore").write_text("*.bin\n", encoding="utf-8")
    (repo.path / ".git" / "info" / "exclude").write_text("*.tmp\n", encoding="utf-8")
    global_excludes = tmp_path / "global-excludes"
    global_excludes.write_text("*.cache\n", encoding="utf-8")
    global_config = tmp_path / "global-config"
    global_config.write_text(f"[core]\n\texcludesFile = {global_excludes.as_posix()}\n", encoding="utf-8")

    paths = {
        name: repo.path / name
        for name in (
            "ignored.log",
            "tracked.log",
            "info.tmp",
            "global.cache",
            "normal.txt",
            "nested/generated.bin",
            "build",
        )
    }
    for name, path in paths.items():
        if name == "build":
            path.mkdir()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
    repo.run("add", "-f", "tracked.log")

    environment = dict(os.environ)
    environment["GIT_CONFIG_GLOBAL"] = os.fspath(global_config)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    service = GitIgnoreService(GitIgnoreRunner(base_environment=environment))
    probe = service.probe(repo.path, is_directory=True)
    assert probe.repository == GitIgnoreRepository(
        repo.path.resolve(),
        False,
        (repo.path / ".git").resolve(),
    )
    assert probe.error is None
    repository = probe.repository
    assert repository is not None

    result = service.ignored(
        repository,
        tuple(IgnoreCandidate(path, is_directory=path.is_dir()) for path in paths.values()),
    )

    assert result.error is None
    assert result.ignored_paths == {
        paths["ignored.log"],
        paths["info.tmp"],
        paths["global.cache"],
        paths["nested/generated.bin"],
        paths["build"],
    }
    assert paths["tracked.log"] not in result.ignored_paths
    assert paths["normal.txt"] not in result.ignored_paths

    inventory = service.inventory(repository, repo.path)
    assert inventory.error is None
    assert {"tracked.log", "normal.txt"} <= set(inventory.included_relative_paths)
    assert {
        "ignored.log",
        "info.tmp",
        "global.cache",
        "nested/generated.bin",
    } <= set(inventory.ignored_relative_paths)
    assert "tracked.log" not in inventory.ignored_relative_paths


def test_git_filesystem_inventory_is_nul_safe_and_literal_subdirectory_scoped(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    selected_root = repository_root / "work[one]"
    selected_root.mkdir(parents=True)
    runner = QueueRunner(
        (
            completed(0, b"work[one]/tracked.txt\0work[one]/line\nbreak.txt\0"),
            completed(0, b"work[one]/generated.log\0work[one]/nested-repo/\0"),
            completed(0, b"work[one]/nested-repo/\0"),
        )
    )
    repository = GitIgnoreRepository(repository_root.resolve(), False)

    inventory = GitIgnoreService(runner).inventory(repository, selected_root)

    assert inventory.error is None
    assert inventory.warning is None
    assert inventory.included_relative_paths == ("tracked.txt", "line\nbreak.txt")
    assert inventory.ignored_relative_paths == ("generated.log", "nested-repo")
    assert inventory.ignored_directory_paths == {"nested-repo"}
    expected_pathspec = ":(top,literal)work[one]"
    assert runner.calls[0][0] == (
        "ls-files",
        "-z",
        "--full-name",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        expected_pathspec,
    )
    assert runner.calls[1][0] == (
        "ls-files",
        "-z",
        "--full-name",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--",
        expected_pathspec,
    )
    assert runner.calls[2][0] == (
        "ls-files",
        "-z",
        "--full-name",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "--",
        expected_pathspec,
    )


def test_streamed_git_inventory_is_deduplicated_and_callback_driven(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    runner = QueueRunner(
        (
            completed(0, b"tracked.txt\0tracked.txt\0line\nbreak.txt\0"),
            completed(0, b"generated.log\0ignored-dir/file.txt\0"),
            completed(0, b"ignored-dir/\0"),
        )
    )
    included: list[str] = []
    ignored: list[tuple[str, bool]] = []

    result = GitIgnoreService(runner).visit_inventory(
        GitIgnoreRepository(repository_root.resolve(), False),
        repository_root,
        included_consumer=included.append,
        ignored_consumer=lambda path, is_directory: ignored.append((path, is_directory)),
    )

    assert result.error is None
    assert result.included_paths == 2
    assert result.ignored_paths == 2
    assert included == ["tracked.txt", "line\nbreak.txt"]
    assert ignored == [("generated.log", False), ("ignored-dir/file.txt", False)]


@pytest.mark.parametrize(
    ("included_output", "ignored_output", "directory_output"),
    [
        (b"tracked.txt\0", b"ignored.log\0", b"unterminated"),
        (b"ignored-dir/child.txt\0", b"ignored.log\0", b"ignored-dir/\0"),
    ],
)
def test_streamed_git_inventory_never_exposes_a_partial_invalid_snapshot(
    tmp_path: Path,
    included_output: bytes,
    ignored_output: bytes,
    directory_output: bytes,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    consumed: list[str] = []
    service = GitIgnoreService(
        QueueRunner(
            (
                completed(0, included_output),
                completed(0, ignored_output),
                completed(0, directory_output),
            )
        )
    )

    result = service.visit_inventory(
        GitIgnoreRepository(repository_root.resolve(), False),
        repository_root,
        included_consumer=consumed.append,
        ignored_consumer=lambda path, _is_directory: consumed.append(path),
    )

    assert result.error is not None
    assert result.error.code == "git_filesystem_inventory_parse_error"
    assert consumed == []


def test_git_filesystem_inventory_keeps_partial_paths_with_a_warning(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    warning = b"warning: could not open directory 'private/': Permission denied\n"
    runner = QueueRunner(
        (
            completed(0, b"visible.txt\0", warning),
            completed(0, b"ignored.log\0", warning),
            completed(0, b"private/\0", warning),
        )
    )

    inventory = GitIgnoreService(runner).inventory(
        GitIgnoreRepository(repository_root.resolve(), False),
        repository_root,
    )

    assert inventory.error is None
    assert inventory.warning is not None
    assert inventory.warning.code == "git_filesystem_inventory_incomplete"
    assert str(inventory.warning) == "could not open directory 'private/': Permission denied"
    assert inventory.included_relative_paths == ("visible.txt",)
    assert inventory.ignored_relative_paths == ("ignored.log",)


@pytest.mark.parametrize(
    ("included", "ignored"),
    [
        (b"unterminated", b""),
        (b"../outside.txt\0", b""),
        (b"inside.txt\0", b"inside.txt\0"),
    ],
)
def test_git_filesystem_inventory_rejects_malformed_or_overlapping_paths(
    tmp_path: Path,
    included: bytes,
    ignored: bytes,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    inventory = GitIgnoreService(QueueRunner((completed(0, included), completed(0, ignored), completed(0)))).inventory(
        GitIgnoreRepository(repository_root.resolve(), False),
        repository_root,
    )

    assert inventory.error is not None
    assert inventory.error.code == "git_filesystem_inventory_parse_error"


def test_git_filesystem_inventory_value_rejects_duplicate_paths() -> None:
    with pytest.raises(ValueError, match="duplicate included"):
        GitFilesystemInventory(("same", "same"))
    with pytest.raises(ValueError, match="duplicate ignored"):
        GitFilesystemInventory((), ("same", "same"))
    with pytest.raises(ValueError, match="ignored directory"):
        GitFilesystemInventory(("other",), ("ignored",), frozenset({"other"}))
    with pytest.raises(ValueError, match="below an ignored directory"):
        GitFilesystemInventory(("ignored/child",), (), frozenset({"ignored"}))


def test_probe_distinguishes_outside_broken_and_bare_repositories(tmp_path: Path) -> None:
    outside_runner = QueueRunner((completed(128, stderr=b"not a repository"),))
    outside = GitIgnoreService(outside_runner).probe(tmp_path, is_directory=True)
    assert outside.repository is None
    assert outside.error is None
    assert "outside" in outside.note

    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "HEAD").write_text("broken\n", encoding="ascii")
    (broken / ".git" / "objects").mkdir()
    (broken / ".git" / "refs").mkdir()
    broken_runner = QueueRunner((completed(128, stderr=b"broken repository"),))
    broken_probe = GitIgnoreService(broken_runner).probe(broken, is_directory=True)
    assert broken_probe.error is not None
    assert broken_probe.error.code == "git_repository_probe_failed"

    bare = tmp_path / "bare.git"
    bare.mkdir()
    bare_runner = QueueRunner(
        (
            completed(0, b"false\ntrue\n"),
            completed(0, os.fsencode(bare) + b"\n"),
        )
    )
    bare_probe = GitIgnoreService(bare_runner).probe(bare, is_directory=True)
    assert bare_probe.repository == GitIgnoreRepository(bare.resolve(), True, bare.resolve())


def test_probe_reports_command_and_parse_failures(tmp_path: Path) -> None:
    missing = GitIgnoreCommandError(
        code="git_not_found_for_ignores",
        message="missing",
        cwd=tmp_path,
        command=("git",),
        unavailable=True,
    )
    unavailable = GitIgnoreService(QueueRunner((missing,))).probe(tmp_path, is_directory=True)
    assert unavailable.error is missing
    assert not unavailable.git_available

    unexpected = GitIgnoreService(QueueRunner((completed(0, b"maybe\n"),))).probe(tmp_path, is_directory=True)
    assert unexpected.error is not None
    assert unexpected.error.code == "git_ignore_parse_error"

    for invalid_root in (b"", b"bad\0root\n"):
        runner = QueueRunner((completed(0, b"true\nfalse\n"), completed(0, invalid_root)))
        result = GitIgnoreService(runner).probe(tmp_path, is_directory=True)
        assert result.error is not None
        assert result.error.code == "git_ignore_parse_error"

    command_error = GitIgnoreCommandError(
        code="git_ignore_command_failed",
        message="failed",
        cwd=tmp_path,
        command=("git",),
    )
    runner = QueueRunner((completed(0, b"true\nfalse\n"), command_error))
    result = GitIgnoreService(runner).probe(tmp_path, is_directory=True)
    assert result.error is command_error


def test_probe_exposes_exact_admin_directory_even_from_inside_non_bare_storage(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    admin = repo.path / ".git"

    probe = GitIgnoreService().probe(admin / "objects", is_directory=True)

    assert probe.error is None
    assert probe.repository == GitIgnoreRepository(admin.resolve(), False, admin.resolve())
    assert "non-bare Git administrative" in probe.note


def test_ignore_matching_handles_empty_external_and_invalid_results(tmp_path: Path) -> None:
    repository = GitIgnoreRepository(tmp_path, False)
    no_call = QueueRunner(())
    empty = GitIgnoreService(no_call).ignored(
        repository,
        (IgnoreCandidate(tmp_path, True),),
    )
    assert empty.ignored_paths == frozenset()
    assert empty.error is None
    assert not no_call.calls

    unmappable = GitIgnoreService(no_call).ignored(
        repository,
        (IgnoreCandidate(tmp_path.parent / "elsewhere", False),),
    )
    assert unmappable.ignored_paths == frozenset()
    assert unmappable.error is not None
    assert unmappable.error.code == "git_ignore_path_mapping_error"
    assert not no_call.calls

    target = tmp_path / "folder"
    command_error = GitIgnoreCommandError(
        code="git_ignore_command_failed",
        message="failed",
        cwd=tmp_path,
        command=("git",),
    )
    failed = GitIgnoreService(QueueRunner((command_error,))).ignored(repository, (IgnoreCandidate(target, True),))
    assert failed.error is command_error

    unknown = GitIgnoreService(QueueRunner((completed(0, b"unknown\0"),))).ignored(
        repository, (IgnoreCandidate(target, True),)
    )
    assert unknown.error is not None
    assert unknown.error.code == "git_ignore_parse_error"

    matched = GitIgnoreService(QueueRunner((completed(0, b"folder/\0"),))).ignored(
        repository, (IgnoreCandidate(target, True),)
    )
    assert matched.ignored_paths == {target}


def test_repository_boundary_helpers_do_not_follow_markers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    assert not has_repository_marker_ancestor(nested)
    assert not is_nested_repository_boundary(nested, selected_root=root)

    linked_admin = tmp_path / "linked-admin"
    (linked_admin / "objects").mkdir(parents=True)
    (linked_admin / "refs").mkdir()
    (linked_admin / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (nested / ".git").write_text(f"gitdir: {linked_admin}\n", encoding="utf-8")
    assert has_repository_marker_ancestor(nested)
    assert is_nested_repository_boundary(nested, selected_root=root)
    assert not is_nested_repository_boundary(root, selected_root=root)
    assert is_git_admin_name(nested / ".git")
    assert has_git_admin_ancestor(nested / ".git" / "objects")

    bare = root / "bare"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    assert looks_like_bare_repository(bare)
    assert is_nested_repository_boundary(bare, selected_root=root)

    (bare / "HEAD").chmod(stat.S_IXUSR)
    assert looks_like_bare_repository(bare)
