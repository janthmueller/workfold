from __future__ import annotations

import os
import stat
from collections.abc import Generator, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast

import pytest
from workfold.collectors.base import DiagnosticSeverity
from workfold.collectors.filesystem import (
    CollectedFilesystemEntry,
    FilesystemAccounting,
    FilesystemCollector,
    TimestampExtractionCoverage,
    scandir_no_follow,
)
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.collectors.ignores import (
    GitFilesystemInventory,
    GitIgnoreCommandError,
    GitIgnoreMatches,
    GitIgnoreProbe,
    GitIgnoreRepository,
    GitIgnoreService,
    IgnoreCandidate,
)
from workfold.coverage import (
    CapabilityStatus,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    SelectionDisposition,
    TimestampCoverageKey,
)
from workfold.models import EntryType, RecordKind, Source, TimestampKind, TimestampObservation

from support.git_repo import GitRepo

FS_MODIFIED = (TimestampKind.FS_MODIFIED,)


def make_admin_layout_nonstandard(repo: GitRepo, storage: Path) -> None:
    storage.mkdir()
    for name in ("objects", "refs"):
        source = repo.path / ".git" / name
        target = storage / name
        source.rename(target)
        source.symlink_to(target, target_is_directory=True)


class FixedIgnoreService(GitIgnoreService):
    def __init__(
        self,
        probe: GitIgnoreProbe,
        matches: GitIgnoreMatches = GitIgnoreMatches(frozenset()),
    ) -> None:
        super().__init__()
        self.probe_result = probe
        self.matches_result = matches
        self.ignored_calls: list[tuple[GitIgnoreRepository, tuple[IgnoreCandidate, ...]]] = []

    def probe(self, path: Path, *, is_directory: bool) -> GitIgnoreProbe:
        return self.probe_result

    def ignored(
        self,
        repository: GitIgnoreRepository,
        candidates: Sequence[IgnoreCandidate],
        *,
        lexical_root: Path | None = None,
    ) -> GitIgnoreMatches:
        del lexical_root
        self.ignored_calls.append((repository, tuple(candidates)))
        return self.matches_result


class NativeOnlyIgnoreService(GitIgnoreService):
    """Force the reference traversal/check-ignore path for equivalence tests."""

    def inventory(self, repository: GitIgnoreRepository, selected_root: Path) -> GitFilesystemInventory:
        return GitFilesystemInventory(
            error=GitIgnoreCommandError(
                code="test_inventory_disabled",
                message="inventory disabled by fixture",
                cwd=selected_root,
                command=("ls-files",),
            )
        )


def test_quick_scan_accounts_for_regular_ignored_excluded_and_admin_entries(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    (repo.path / ".gitignore").write_text("ignored/\n*.ignored\n", encoding="utf-8")
    (repo.path / "ordinary" / "nested").mkdir(parents=True)
    (repo.path / "ordinary" / "nested" / "work.txt").write_text("work", encoding="utf-8")
    (repo.path / "ignored").mkdir()
    (repo.path / "ignored" / "hidden.txt").write_text("hidden", encoding="utf-8")
    (repo.path / "one.ignored").write_text("ignored", encoding="utf-8")
    (repo.path / "excluded").mkdir()
    (repo.path / "excluded" / "child.txt").write_text("excluded", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "must-not-be-seen.txt").write_text("private", encoding="utf-8")
    (repo.path / "external-link").symlink_to(external, target_is_directory=True)

    result = FilesystemCollector().collect(
        (repo.path,),
        timestamp_kinds=FS_MODIFIED,
        exclusions=("excluded/", "*.ignored"),
    )

    by_relative: dict[str, CollectedFilesystemEntry] = {}
    for item in result.entries:
        path = item.origin.path
        assert path is not None
        by_relative[path.relative_to(repo.path).as_posix() if path != repo.path else "."] = item
    assert by_relative["ordinary/nested/work.txt"].disposition is RecordDisposition.ELIGIBLE
    assert "ordinary" not in by_relative
    assert "ordinary/nested" not in by_relative
    assert "ignored" not in by_relative
    assert "ignored/hidden.txt" not in by_relative
    assert "one.ignored" not in by_relative
    assert by_relative["excluded/child.txt"].disposition is RecordDisposition.EXPLICITLY_EXCLUDED
    assert by_relative[".git"].disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert by_relative["external-link"].disposition is RecordDisposition.EXCLUDED_ENTRY_TYPE
    assert "external-link/must-not-be-seen.txt" not in by_relative
    assert all(
        item.origin.entry_type is EntryType.REGULAR_FILE
        for item in result.entries
        if item.disposition is RecordDisposition.ELIGIBLE
    )
    assert len(result.observations) == len(result.eligible_origins)
    assert all(item.kind is TimestampKind.FS_MODIFIED for item in result.observations)
    assert not result.is_partial
    records = result.accounting.records[0]
    records.validate()
    assert records.ignored == 1
    assert records.explicitly_excluded == 2
    assert result.accounting.timestamps[0].requested == result.accounting.records[0].eligible


def test_git_inventory_validates_current_files_without_statting_ignored_tree(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    ignored = repo.path / "ignored"
    ignored.mkdir()
    (repo.path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    tracked = repo.path / "tracked.txt"
    tracked.write_text("tracked", encoding="utf-8")
    tracked_ignored = repo.path / "tracked.log"
    tracked_ignored.write_text("tracked despite current ignore rules", encoding="utf-8")
    tracked_inside_ignored = ignored / "tracked.txt"
    tracked_inside_ignored.write_text("tracked below ignored directory", encoding="utf-8")
    staged = repo.path / "staged.txt"
    staged.write_text("staged", encoding="utf-8")
    deleted = repo.path / "deleted.txt"
    deleted.write_text("soon absent", encoding="utf-8")
    repo.run("add", ".gitignore", "tracked.txt", "staged.txt", "deleted.txt")
    repo.run("add", "-f", "tracked.log", "ignored/tracked.txt")
    tracked.write_text("unstaged current contents", encoding="utf-8")
    deleted.unlink()
    untracked = repo.path / "untracked.txt"
    untracked.write_text("never staged", encoding="utf-8")
    for index in range(200):
        (ignored / f"generated-{index}.txt").write_text("ignored", encoding="utf-8")

    lstat_calls: list[Path] = []

    def counting_lstat(path: Path) -> os.stat_result:
        lstat_calls.append(path)
        return os.lstat(path)

    result = FilesystemCollector(lstat_reader=counting_lstat).collect(
        (repo.path,),
        timestamp_kinds=FS_MODIFIED,
    )

    eligible_paths = {item.path for item in result.eligible_origins}
    assert eligible_paths == {
        repo.path / ".gitignore",
        tracked,
        tracked_ignored,
        tracked_inside_ignored,
        staged,
        untracked,
    }
    assert deleted not in {item.origin.path for item in result.entries}
    assert {path for path in lstat_calls if ignored == path or ignored in path.parents} == {tracked_inside_ignored}
    assert result.accounting.records[0].ignored == 200
    assert len(result.observations) == 6
    assert not result.diagnostics
    result.accounting.records[0].validate()


def test_git_inventory_restricts_candidates_to_literal_selected_subdirectory(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    selected = repo.path / "work[one]"
    selected.mkdir()
    (repo.path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    inside = selected / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    unusual = selected / "line\nbreak.txt"
    unusual.write_text("unusual", encoding="utf-8")
    (selected / "ignored.tmp").write_text("ignored", encoding="utf-8")
    (repo.path / "outside.txt").write_text("outside", encoding="utf-8")

    result = FilesystemCollector().collect((selected,), timestamp_kinds=FS_MODIFIED)

    assert {item.path for item in result.eligible_origins} == {inside, unusual}
    assert result.accounting.records[0].ignored == 1
    assert all(item.origin.repository_or_root == selected for item in result.entries)
    assert not result.diagnostics


def test_git_inventory_prunes_ignored_directories_but_keeps_visible_empty_directories(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    ignored = repo.path / "ignored"
    ignored.mkdir()
    visible_empty = repo.path / "visible-empty"
    visible_empty.mkdir()
    (repo.path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    for index in range(100):
        (ignored / f"artifact-{index}.txt").write_text("ignored", encoding="utf-8")

    opened: list[Path] = []

    def guarded_scandir(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if path == ignored:
            raise AssertionError("Git-ignored directory must be pruned before traversal")
        opened.append(path)
        return os.scandir(path)

    result = FilesystemCollector(scandir_reader=guarded_scandir).collect(
        (repo.path,),
        timestamp_kinds=FS_MODIFIED,
        include_directories=True,
    )

    assert visible_empty in {item.path for item in result.eligible_origins}
    assert ignored not in opened
    assert result.accounting.records[0].ignored == 101
    assert not result.diagnostics


def test_git_inventory_and_native_scan_select_the_same_regular_files(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    (repo.path / "nested" / "generated").mkdir(parents=True)
    visible = {
        repo.path / ".gitignore",
        repo.path / "tracked.log",
        repo.path / "nested" / "work.txt",
        repo.path / "untracked.txt",
    }
    for path in visible:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("visible", encoding="utf-8")
    (repo.path / ".gitignore").write_text("*.log\nnested/generated/\n", encoding="utf-8")
    repo.run("add", ".gitignore", "nested/work.txt")
    repo.run("add", "-f", "tracked.log")
    (repo.path / "ignored.log").write_text("ignored", encoding="utf-8")
    (repo.path / "nested" / "generated" / "artifact.txt").write_text("ignored", encoding="utf-8")

    fast = FilesystemCollector().collect((repo.path,), timestamp_kinds=FS_MODIFIED)
    native = FilesystemCollector(ignore_service=NativeOnlyIgnoreService()).collect(
        (repo.path,), timestamp_kinds=FS_MODIFIED
    )

    native_regular = {item.path for item in native.eligible_origins}
    assert {item.path for item in fast.eligible_origins} == native_regular == visible
    assert fast.accounting.records == native.accounting.records
    assert [
        (item.key, item.requested, item.captured, item.unavailable, item.unsupported, item.errors)
        for item in fast.accounting.timestamps
    ] == [
        (item.key, item.requested, item.captured, item.unavailable, item.unsupported, item.errors)
        for item in native.accounting.timestamps
    ]


def test_explicitly_excluded_directory_is_recorded_once_and_never_opened(tmp_path: Path) -> None:
    root = tmp_path / "root"
    excluded = root / "private"
    excluded.mkdir(parents=True)
    (excluded / "unreadable.txt").write_text("private", encoding="utf-8")
    (root / "visible.txt").write_text("visible", encoding="utf-8")

    def guarded_scandir(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if path == excluded:
            raise AssertionError("an explicitly excluded directory must be pruned")
        return os.scandir(path)

    result = FilesystemCollector(scandir_reader=guarded_scandir).collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        exclusions=("private/",),
        respect_gitignore=False,
        include_ignored=True,
    )

    excluded_entries = [item for item in result.entries if item.disposition is RecordDisposition.EXPLICITLY_EXCLUDED]
    assert [item.origin.path for item in excluded_entries] == [excluded]
    assert all(item.origin.path != excluded / "unreadable.txt" for item in result.entries)
    assert not result.diagnostics
    assert result.accounting.records[0].explicitly_excluded == 1


def test_exhaustive_scan_includes_directories_and_symlinks_without_following(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "file.txt").write_text("work", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    result = FilesystemCollector().collect(
        (root,),
        timestamp_kinds=tuple(
            (
                TimestampKind.FS_CREATED,
                TimestampKind.FS_MODIFIED,
                TimestampKind.FS_METADATA_CHANGED,
                TimestampKind.FS_ACCESSED,
            )
        ),
        include_directories=True,
        include_symlinks=True,
        respect_gitignore=False,
        include_ignored=True,
    )

    paths = {item.origin.path for item in result.entries}
    assert paths == {root, child, child / "file.txt", root / "link"}
    assert outside / "secret.txt" not in paths
    assert {item.origin.entry_type for item in result.entries} == {
        EntryType.DIRECTORY,
        EntryType.REGULAR_FILE,
        EntryType.SYMLINK,
    }
    by_kind = {item.key.timestamp_kind: item for item in result.accounting.timestamps}
    eligible = result.accounting.records[0].eligible
    assert all(item.requested == eligible for item in by_kind.values())
    assert by_kind[TimestampKind.FS_MODIFIED].captured == eligible
    assert by_kind[TimestampKind.FS_ACCESSED].captured == eligible
    birth = by_kind[TimestampKind.FS_CREATED]
    birth_capability = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_CREATED)
    if birth_capability.status is CapabilityStatus.SUPPORTED:
        assert birth.captured + birth.unavailable == eligible
        assert birth.unsupported == 0
    else:
        assert birth.unsupported == eligible
    atime = next(item for item in result.capabilities if item.timestamp_kind is TimestampKind.FS_ACCESSED)
    assert atime.status is CapabilityStatus.POTENTIALLY_UNRELIABLE


def test_entry_type_scope_can_exclude_regular_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    file_path = child / "file.txt"
    file_path.write_text("work", encoding="utf-8")

    result = FilesystemCollector().collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        include_regular_files=False,
        include_directories=True,
        respect_gitignore=False,
        include_ignored=True,
    )

    eligible = tuple(item.origin for item in result.entries if item.disposition is RecordDisposition.ELIGIBLE)
    assert {item.path for item in eligible} == {root, child}
    assert all(item.entry_type is EntryType.DIRECTORY for item in eligible)
    assert next(item for item in result.entries if item.origin.path == file_path).disposition is (
        RecordDisposition.EXCLUDED_ENTRY_TYPE
    )


def test_exact_file_and_symlink_roots_are_not_expanded(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    quick = FilesystemCollector().collect(
        (target, link),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert [item.origin.path for item in quick.entries] == [target, link]
    assert [item.disposition for item in quick.entries] == [
        RecordDisposition.ELIGIBLE,
        RecordDisposition.EXCLUDED_ENTRY_TYPE,
    ]

    exhaustive = FilesystemCollector().collect(
        (link,),
        timestamp_kinds=FS_MODIFIED,
        include_symlinks=True,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert exhaustive.eligible_origins[0].entry_type is EntryType.SYMLINK
    assert exhaustive.observations[0].origin.path == link


def test_lexical_overlaps_deduplicate_but_nested_repository_root_remains_exact(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "outer.txt").write_text("outer", encoding="utf-8")
    nested = GitRepo.create(outer / "nested")
    (nested.path / "nested.txt").write_text("nested", encoding="utf-8")

    result = FilesystemCollector().collect(
        (outer, outer, outer / "outer.txt", nested.path),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )

    assert result.requested_roots == (outer, outer, outer / "outer.txt", nested.path)
    assert result.scan_roots == (outer, nested.path)
    assert result.successful_roots == (outer, nested.path)
    assert result.overlapping_roots_deduplicated == 2
    assert len(result.accounting.records) == 2
    outer_nested = next(
        item for item in result.entries if item.origin.repository_or_root == outer and item.origin.path == nested.path
    )
    assert outer_nested.disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    nested_file = next(
        item
        for item in result.entries
        if item.origin.repository_or_root == nested.path and item.origin.path == nested.path / "nested.txt"
    )
    assert nested_file.disposition is RecordDisposition.ELIGIBLE


def test_missing_roots_and_traversal_failures_are_structured_partial_results(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing_result = FilesystemCollector().collect(
        (missing,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert missing_result.successful_roots == ()
    assert missing_result.scan_roots == (missing,)
    assert missing_result.accounting.records == ()
    assert missing_result.is_partial
    assert missing_result.diagnostics[0].code == "path_not_found"

    root = tmp_path / "root"
    root.mkdir()

    @contextmanager
    def denied_scandir(path: Path) -> Generator[Iterator[os.DirEntry[str]], None, None]:
        raise PermissionError(f"denied: {path}")
        yield iter(())

    traversal = FilesystemCollector(scandir_reader=denied_scandir).collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert traversal.is_partial
    assert traversal.diagnostics[0].code == "filesystem_traversal_error"
    assert traversal.accounting.records[0].discovered == 0


def test_descendant_stat_failures_receive_record_error_accounting(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    class BrokenEntry:
        name = "broken"

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            assert follow_symlinks is False
            raise PermissionError("cannot stat")

    @contextmanager
    def broken_scandir(path: Path) -> Generator[Iterator[os.DirEntry[str]], None, None]:
        assert path == root
        entries = cast(Iterator[os.DirEntry[str]], iter((BrokenEntry(),)))
        yield entries

    result = FilesystemCollector(scandir_reader=broken_scandir).collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )

    coverage = result.accounting.records[0]
    assert coverage.discovered == 1
    assert coverage.record_errors == 1
    assert result.is_partial
    assert result.diagnostics[0].code == "filesystem_stat_error"


def test_ignore_unavailability_is_error_in_visible_repo_but_warning_outside(tmp_path: Path) -> None:
    missing = GitIgnoreCommandError(
        code="git_not_found_for_ignores",
        message="Git is unavailable",
        cwd=tmp_path,
        command=("git",),
        unavailable=True,
    )
    probe = GitIgnoreProbe(None, False, "Git unavailable", missing)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("work", encoding="utf-8")
    outside_result = FilesystemCollector(ignore_service=FixedIgnoreService(probe)).collect(
        (outside,), timestamp_kinds=FS_MODIFIED
    )
    assert outside_result.diagnostics[0].severity is DiagnosticSeverity.WARNING
    assert not outside_result.is_partial
    outside_ignore = next(item for item in outside_result.capabilities if item.timestamp_kind is None)
    assert outside_ignore.status is CapabilityStatus.UNAVAILABLE

    visible = tmp_path / "visible"
    (visible / ".git").mkdir(parents=True)
    (visible / ".git" / "HEAD").write_text("broken\n", encoding="ascii")
    (visible / ".git" / "objects").mkdir()
    (visible / ".git" / "refs").mkdir()
    (visible / "file.txt").write_text("work", encoding="utf-8")
    visible_result = FilesystemCollector(ignore_service=FixedIgnoreService(probe)).collect(
        (visible,), timestamp_kinds=FS_MODIFIED
    )
    assert visible_result.diagnostics[0].severity is DiagnosticSeverity.ERROR
    assert visible_result.is_partial


def test_losing_git_after_repository_probe_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("work", encoding="utf-8")
    repository = GitIgnoreRepository(root, False)
    unavailable = GitIgnoreCommandError(
        code="git_not_found_for_ignores",
        message="Git vanished",
        cwd=root,
        command=("git",),
        unavailable=True,
    )
    service = FixedIgnoreService(
        GitIgnoreProbe(repository, True, "repository"),
        GitIgnoreMatches(frozenset(), unavailable),
    )

    result = FilesystemCollector(ignore_service=service).collect((root,), timestamp_kinds=FS_MODIFIED)

    assert result.is_partial
    assert result.diagnostics[0].severity is DiagnosticSeverity.ERROR
    assert service.ignored_calls


def test_include_ignored_is_an_explicit_complete_policy_even_without_git(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "ignored.log").write_text("included", encoding="utf-8")
    missing = GitIgnoreCommandError(
        code="git_not_found_for_ignores",
        message="Git unavailable",
        cwd=root,
        command=("git",),
        unavailable=True,
    )
    service = FixedIgnoreService(GitIgnoreProbe(None, False, "unavailable", missing))

    result = FilesystemCollector(ignore_service=service).collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )

    assert not result.diagnostics
    assert not service.ignored_calls
    assert any(item.origin.path == root / "ignored.log" for item in result.entries)
    ignore_capability = next(item for item in result.capabilities if item.timestamp_kind is None)
    assert ignore_capability.status is CapabilityStatus.SUPPORTED
    assert ignore_capability.note == "ignored entries were explicitly included"


def test_unsupported_unavailable_and_error_timestamp_slots_reconcile(tmp_path: Path) -> None:
    unavailable_path = tmp_path / "unavailable"
    unavailable_path.write_text("work", encoding="utf-8")
    missing_birth = cast(os.stat_result, type("MissingBirthStat", (), {"st_mode": stat.S_IFREG})())

    def missing_birth_lstat(path: Path) -> os.stat_result:
        assert path == unavailable_path
        return missing_birth

    unavailable = FilesystemCollector(
        timestamp_adapter=FilesystemTimestampAdapter(platform_name="darwin", created_supported=True),
        lstat_reader=missing_birth_lstat,
    ).collect(
        (unavailable_path,),
        timestamp_kinds=(TimestampKind.FS_CREATED,),
        respect_gitignore=False,
        include_ignored=True,
    )
    assert unavailable.accounting.timestamps[0].unavailable == 1
    assert not unavailable.diagnostics

    error_path = tmp_path / "error"
    error_path.write_text("work", encoding="utf-8")
    extreme = cast(
        os.stat_result,
        type("ExtremeStat", (), {"st_mode": stat.S_IFREG, "st_mtime_ns": 10**30})(),
    )

    def extreme_lstat(path: Path) -> os.stat_result:
        assert path == error_path
        return extreme

    error = FilesystemCollector(lstat_reader=extreme_lstat).collect(
        (error_path,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert error.accounting.timestamps[0].errors == 1
    assert error.diagnostics[0].code == "filesystem_timestamp_error"
    assert error.is_partial


def test_coverage_adapter_requires_complete_selection_and_plotting_maps(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.txt").write_text("one", encoding="utf-8")
    (root / "two.txt").write_text("two", encoding="utf-8")
    result = FilesystemCollector().collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    observation_ids = [item.observation_id for item in result.observations]
    assert len(observation_ids) == 2
    selection = {
        observation_ids[0]: SelectionDisposition.INCLUDED,
        observation_ids[1]: SelectionDisposition.OUTSIDE_DATE,
    }
    plotting = {observation_ids[0]: PlottingDisposition.MARKER}

    ledger = result.build_coverage(selection, plotting)
    ledger.validate()
    assert ledger.records[0].eligible == 2
    assert ledger.timestamps[0].requested == 2
    assert ledger.timestamps[0].included == 1
    assert ledger.timestamps[0].outside_date == 1
    assert ledger.timestamps[0].markers == 1
    with pytest.raises(ValueError, match="selection map"):
        result.build_coverage({}, {})
    with pytest.raises(ValueError, match="selection map"):
        result.build_coverage(
            {**selection, "invented": SelectionDisposition.OUTSIDE_DATE},
            plotting,
        )
    with pytest.raises(ValueError, match="plotting map"):
        result.build_coverage(selection, {})
    with pytest.raises(ValueError, match="plotting map"):
        result.build_coverage(
            selection,
            {**plotting, observation_ids[1]: PlottingDisposition.MARKER},
        )


def test_accounting_value_objects_reject_nonconservation() -> None:
    target = "/root"
    record_key = RecordCoverageKey(Source.FILESYSTEM, target, RecordKind.FILESYSTEM_ENTRY)
    timestamp_key = TimestampCoverageKey(
        Source.FILESYSTEM,
        target,
        RecordKind.FILESYSTEM_ENTRY,
        TimestampKind.FS_MODIFIED,
    )
    with pytest.raises(ValueError, match="non-negative"):
        TimestampExtractionCoverage(timestamp_key, -1, 0, 0, 0, 0, ())
    with pytest.raises(ValueError, match="does not reconcile"):
        TimestampExtractionCoverage(timestamp_key, 1, 0, 0, 0, 0, ())
    with pytest.raises(ValueError, match="captured count"):
        TimestampExtractionCoverage(timestamp_key, 1, 1, 0, 0, 0, ())
    with pytest.raises(ValueError, match="duplicate observations"):
        TimestampExtractionCoverage(timestamp_key, 2, 2, 0, 0, 0, ("same", "same"))

    record = RecordCoverage(record_key, discovered=1, eligible=1)
    extraction = TimestampExtractionCoverage(timestamp_key, 1, 1, 0, 0, 0, ("observation",))
    with pytest.raises(ValueError, match="duplicate keys"):
        FilesystemAccounting((record, record), (extraction,))
    with pytest.raises(ValueError, match="duplicate keys"):
        FilesystemAccounting((record,), (extraction, extraction))
    with pytest.raises(ValueError, match="no record partition"):
        FilesystemAccounting((), (extraction,))
    mismatched_record = RecordCoverage(record_key, discovered=2, eligible=2)
    with pytest.raises(ValueError, match="must equal eligible"):
        FilesystemAccounting((mismatched_record,), (extraction,))
    assert extraction.count(ExtractionDisposition.CAPTURED) == 1


def test_collection_rejects_invalid_scope_configuration(tmp_path: Path) -> None:
    collector = FilesystemCollector()
    with pytest.raises(ValueError, match="at least one path"):
        collector.collect((), timestamp_kinds=FS_MODIFIED)
    with pytest.raises(ValueError, match="ignore policy"):
        collector.collect((tmp_path,), timestamp_kinds=FS_MODIFIED, respect_gitignore=True, include_ignored=True)
    with pytest.raises(ValueError, match="ignore policy"):
        collector.collect((tmp_path,), timestamp_kinds=FS_MODIFIED, respect_gitignore=False, include_ignored=False)
    with pytest.raises(ValueError, match="at least one timestamp"):
        collector.collect((tmp_path,), timestamp_kinds=(), respect_gitignore=False, include_ignored=True)
    with pytest.raises(ValueError, match="only filesystem"):
        collector.collect(
            (tmp_path,),
            timestamp_kinds=(TimestampKind.GIT_AUTHOR,),
            respect_gitignore=False,
            include_ignored=True,
        )


def test_git_admin_and_bare_roots_are_semantically_excluded(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    admin_file = repo.path / ".git" / "HEAD"
    admin = FilesystemCollector().collect(
        (admin_file,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert admin.entries[0].disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert not admin.observations

    bare = tmp_path / "bare"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    bare_result = FilesystemCollector().collect(
        (bare,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )
    assert bare_result.entries[0].disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert len(bare_result.entries) == 1


def test_ignore_evaluation_maps_symlinked_ancestors_but_keeps_lexical_provenance(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    (repo.path / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    ignored = repo.path / "private.ignored"
    ignored.write_text("private", encoding="utf-8")
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo.path, target_is_directory=True)
    selected = alias / ignored.name

    result = FilesystemCollector().collect((selected,), timestamp_kinds=FS_MODIFIED)

    assert not result.diagnostics
    assert len(result.entries) == 1
    assert result.entries[0].origin.path == selected
    assert result.entries[0].origin.repository_or_root == selected
    assert result.entries[0].disposition is RecordDisposition.IGNORED
    assert not result.observations


def test_symlinked_ancestor_into_git_admin_is_semantically_excluded(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    make_admin_layout_nonstandard(repo, tmp_path / "admin-storage")
    admin_alias = tmp_path / "admin-alias"
    admin_alias.symlink_to(repo.path / ".git", target_is_directory=True)
    selected = admin_alias / "config"

    result = FilesystemCollector().collect(
        (selected,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )

    assert not result.diagnostics
    assert len(result.entries) == 1
    assert result.entries[0].origin.path == selected
    assert result.entries[0].disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert not result.observations


def test_confirmed_admin_directory_is_pruned_with_nonstandard_internal_layout(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    make_admin_layout_nonstandard(repo, tmp_path / "admin-storage")

    result = FilesystemCollector().collect(
        (repo.path,),
        timestamp_kinds=FS_MODIFIED,
        include_directories=True,
        include_symlinks=True,
        respect_gitignore=False,
        include_ignored=True,
    )

    admin_entries = [item for item in result.entries if item.origin.path == repo.path / ".git"]
    assert len(admin_entries) == 1
    assert admin_entries[0].disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert all(
        item.origin.path is None or repo.path / ".git" not in item.origin.path.parents for item in result.entries
    )


def test_cached_admin_boundary_prunes_nonstandard_storage_inside_worktree(tmp_path: Path) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    admin_storage = repo.path / "git-storage"
    (repo.path / ".git").rename(admin_storage)
    (repo.path / ".git").write_text("gitdir: git-storage\n", encoding="utf-8")
    (repo.path / "visible.txt").write_text("visible", encoding="utf-8")

    result = FilesystemCollector().collect(
        (repo.path,),
        timestamp_kinds=FS_MODIFIED,
        include_directories=True,
        include_symlinks=True,
        respect_gitignore=False,
        include_ignored=True,
    )

    storage = next(item for item in result.entries if item.origin.path == admin_storage)
    assert storage.disposition is RecordDisposition.SEMANTIC_GIT_ADMIN
    assert all(item.origin.path is None or admin_storage not in item.origin.path.parents for item in result.entries)


def test_unrelated_dot_git_entries_remain_normal_filesystem_evidence(tmp_path: Path) -> None:
    root = tmp_path / "ordinary"
    dot_git_directory = root / "nested" / ".git"
    dot_git_directory.mkdir(parents=True)
    note = dot_git_directory / "notes.txt"
    note.write_text("not a repository", encoding="utf-8")
    dot_git_file = root / "other" / ".git"
    dot_git_file.parent.mkdir()
    dot_git_file.write_text("ordinary data", encoding="utf-8")

    result = FilesystemCollector().collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        include_directories=True,
        respect_gitignore=False,
        include_ignored=True,
    )

    by_path = {item.origin.path: item.disposition for item in result.entries}
    assert by_path[dot_git_directory] is RecordDisposition.ELIGIBLE
    assert by_path[note] is RecordDisposition.ELIGIBLE
    assert by_path[dot_git_file] is RecordDisposition.ELIGIBLE
    assert all(item is not RecordDisposition.SEMANTIC_GIT_ADMIN for item in by_path.values())


def test_queued_directory_replaced_by_symlink_cannot_escape_scan_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    escape = root / "escape"
    trigger = root / "trigger"
    escape.mkdir(parents=True)
    trigger.mkdir(parents=True)
    (escape / "original.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "must-not-be-collected.txt"
    secret.write_text("outside", encoding="utf-8")
    displaced = root / "displaced"
    swapped = False

    def swapping_scandir(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        nonlocal swapped
        if path == trigger and not swapped:
            escape.rename(displaced)
            escape.symlink_to(outside, target_is_directory=True)
            swapped = True
        return scandir_no_follow(path)

    result = FilesystemCollector(scandir_reader=swapping_scandir).collect(
        (root,),
        timestamp_kinds=FS_MODIFIED,
        respect_gitignore=False,
        include_ignored=True,
    )

    assert swapped
    assert secret not in {item.origin.path for item in result.entries}
    assert all(item.origin.path != escape / secret.name for item in result.entries)
    assert result.is_partial
    assert any(item.code == "filesystem_concurrent_mutation" for item in result.diagnostics)


def test_non_retaining_collection_streams_one_entry_batch_at_a_time(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (root / name).write_text(name, encoding="utf-8")
    received: list[tuple[TimestampObservation, ...]] = []

    result = FilesystemCollector().collect(
        (root,),
        timestamp_kinds=(TimestampKind.FS_MODIFIED, TimestampKind.FS_ACCESSED),
        respect_gitignore=False,
        include_ignored=True,
        observation_consumer=received.append,
        retain_entries=False,
        retain_observations=False,
    )

    assert result.entries == result.observations == ()
    assert len(received) == 3
    assert all(len(batch) == 2 for batch in received)
    assert all(len({item.origin.record_id for item in batch}) == 1 for batch in received)
    assert all(not item.observation_ids_complete for item in result.accounting.timestamps)
    assert sum(item.captured for item in result.accounting.timestamps) == 6
    with pytest.raises(ValueError, match="non-retaining"):
        result.build_coverage({}, {})
