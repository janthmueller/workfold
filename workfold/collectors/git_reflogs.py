"""Semantic local reflog collection, including non-commit object updates.

Git's formatted ``reflog show`` is built on revision walking and silently omits
entries whose new object is not a commit.  Workfold therefore asks Git which
reflogs exist and resolves each semantic log path with ``rev-parse --git-path``
before parsing the documented reflog record representation.  These files are
never treated as filesystem activity metadata.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity
from workfold.collectors.git import GitCommandError, GitRepository, GitRunner
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_reflog_id

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_EPOCH_RE: Final[re.Pattern[bytes]] = re.compile(rb"-?[0-9]+\Z")
_OFFSET_RE: Final[re.Pattern[bytes]] = re.compile(rb"[+-][0-9]{4}\Z")
_MIN_LOCALIZABLE_EPOCH: Final[int] = -62_135_510_400
_MAX_LOCALIZABLE_EPOCH: Final[int] = 253_402_214_399
_PER_WORKTREE_REF_PREFIXES: Final[tuple[str, ...]] = (
    "refs/bisect/",
    "refs/rewritten/",
    "refs/worktree/",
)


class GitReflogParseError(ValueError):
    """A structured failure to parse reflog discovery or semantic records."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        ref_name: str | None = None,
        record_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.ref_name = ref_name
        self.record_count = record_count


class GitReflogReadError(OSError):
    """A structured failure to resolve or safely read one semantic reflog."""

    def __init__(self, code: str, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class ReflogRef:
    """Availability and extraction counts for one repository reflog."""

    repository: GitRepository
    ref_name: str
    entry_count: int
    captured_entry_count: int

    @property
    def unavailable_entry_count(self) -> int:
        """Return inventoried records whose timestamp could not be captured."""

        return max(0, self.entry_count - self.captured_entry_count)


@dataclass(frozen=True, slots=True)
class ParsedReflogEntry:
    """One exact timestamp-bearing record from a semantic reflog."""

    ref_name: str
    raw_ref_name: bytes
    raw_selector: str
    raw_selector_bytes: bytes
    new_id: str
    old_id: str
    epoch_seconds: int
    offset_seconds: int
    raw_timestamp: str
    raw_timestamp_bytes: bytes
    actor_name: str
    raw_actor_name: bytes
    actor_email: str
    raw_actor_email: bytes
    raw_actor: str
    raw_actor_bytes: bytes
    message: str
    raw_message: bytes
    duplicate_ordinal: int

    @property
    def epoch_nanoseconds(self) -> int:
        return self.epoch_seconds * 1_000_000_000


@dataclass(frozen=True, slots=True)
class ReflogVisit:
    """Counts and snapshot state from one bounded semantic reflog visit."""

    entry_count: int
    captured_entry_count: int
    changed_during_read: bool


@dataclass(frozen=True, slots=True)
class _ParsedReflogLine:
    old_id: bytes
    new_id: bytes
    identity: bytes
    epoch: bytes
    offset: bytes
    actor_name: bytes
    actor_email: bytes
    message: bytes
    epoch_seconds: int
    offset_seconds: int

    @property
    def duplicate_key(self) -> tuple[bytes, bytes, bytes, bytes, bytes]:
        return (
            self.old_id,
            self.new_id,
            self.identity,
            self.epoch + b" " + self.offset,
            self.message,
        )

    def to_entry(
        self,
        *,
        ref_name: str,
        raw_ref_name: bytes,
        selector_index: int,
        duplicate_ordinal: int,
    ) -> ParsedReflogEntry:
        raw_timestamp = self.epoch + b" " + self.offset
        raw_selector = raw_ref_name + b"@{" + str(selector_index).encode("ascii") + b"}"
        return ParsedReflogEntry(
            ref_name=ref_name,
            raw_ref_name=raw_ref_name,
            raw_selector=raw_selector.decode("utf-8", errors="surrogateescape"),
            raw_selector_bytes=raw_selector,
            new_id=self.new_id.decode("ascii"),
            old_id=self.old_id.decode("ascii"),
            epoch_seconds=self.epoch_seconds,
            offset_seconds=self.offset_seconds,
            raw_timestamp=raw_timestamp.decode("ascii"),
            raw_timestamp_bytes=raw_timestamp,
            actor_name=self.actor_name.decode("utf-8", errors="surrogateescape"),
            raw_actor_name=self.actor_name,
            actor_email=self.actor_email.decode("utf-8", errors="surrogateescape"),
            raw_actor_email=self.actor_email,
            raw_actor=self.identity.decode("utf-8", errors="surrogateescape"),
            raw_actor_bytes=self.identity,
            message=self.message.decode("utf-8", errors="surrogateescape"),
            raw_message=self.message,
            duplicate_ordinal=duplicate_ordinal,
        )


@dataclass(frozen=True, slots=True)
class CollectedGitReflog:
    """A semantic reflog entry paired with its repository."""

    repository: GitRepository
    entry: ParsedReflogEntry

    def to_origin(self) -> RecordOrigin:
        """Convert the raw entry to a provenance-preserving domain record."""

        return RecordOrigin(
            record_id=git_reflog_id(
                self.repository.root,
                self.entry.ref_name,
                self.entry.old_id,
                self.entry.new_id,
                self.entry.raw_selector,
                self.entry.raw_timestamp,
                self.entry.raw_actor,
                self.entry.message,
                self.entry.duplicate_ordinal,
            ),
            source=Source.GIT,
            record_kind=RecordKind.REFLOG,
            repository_or_root=self.repository.root,
            object_id=self.entry.new_id,
            target_id=self.entry.old_id,
            ref_name=self.entry.ref_name,
            description=self.entry.message,
        )

    def to_observation(self) -> TimestampObservation:
        """Convert the exact reflog timestamp to a normalized observation."""

        return TimestampObservation.create(
            self.to_origin(),
            TimestampKind.GIT_REFLOG,
            self.entry.epoch_nanoseconds,
            self.entry.raw_timestamp,
            original_offset_minutes=self.entry.offset_seconds // 60,
            actor_name=self.entry.actor_name,
            actor_email=self.entry.actor_email,
        )


@dataclass(frozen=True, slots=True)
class GitReflogCollectionResult:
    """Reflog records plus per-ref availability accounting."""

    entries: tuple[CollectedGitReflog, ...]
    available_refs: tuple[ReflogRef, ...]
    refs_without_reflog: tuple[ReflogRef, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_repositories: int
    successful_repositories: int
    discovered_refs: int
    captured_entries: int
    unavailable_entries: int
    parse_errors: int
    records_retained: bool = True

    def __post_init__(self) -> None:
        if self.records_retained and len(self.entries) != self.captured_entries:
            raise ValueError("retained reflog entries do not match captured entry accounting")
        if len(self.entries) > self.captured_entries:
            raise ValueError("retained reflog entries exceed captured entry accounting")

    @property
    def is_partial(self) -> bool:
        return any(item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics)


def parse_current_refs(payload: bytes) -> tuple[str, ...]:
    """Parse ``show-ref --head`` output and return unique current ref names."""

    refs: list[str] = []
    seen: set[str] = set()
    for line in payload.splitlines():
        oid_raw, separator, ref_raw = line.partition(b" ")
        if not separator or _OID_RE.fullmatch(oid_raw) is None or not ref_raw:
            raise GitReflogParseError("invalid_git_ref_list", "show-ref returned a malformed ref")
        if b"\0" in ref_raw:
            raise GitReflogParseError("invalid_git_ref_list", "show-ref returned a NUL in a ref")
        ref_name = ref_raw.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    if "HEAD" not in seen:
        refs.insert(0, "HEAD")
    return tuple(refs)


def parse_reflog_list(payload: bytes) -> tuple[str, ...]:
    """Parse exact local ref names returned by ``git reflog list``."""

    refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in payload.splitlines():
        if not raw_ref or b"\0" in raw_ref:
            raise GitReflogParseError("invalid_git_reflog_list", "git reflog list returned an invalid ref")
        ref_name = raw_ref.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    return tuple(refs)


def parse_reflog_selectors(payload: bytes) -> tuple[str, ...]:
    """Parse full ``%gD`` selectors used by the pre-2.45 fallback."""

    refs: list[str] = []
    seen: set[str] = set()
    for raw_selector in payload.splitlines():
        raw_ref, separator, ordinal = raw_selector.rpartition(b"@{")
        if not separator or not raw_ref or not ordinal.endswith(b"}") or b"\0" in raw_ref:
            raise GitReflogParseError(
                "invalid_git_reflog_list",
                "git reflog fallback returned an invalid selector",
            )
        ref_name = raw_ref.decode("utf-8", errors="surrogateescape")
        if ref_name not in seen:
            refs.append(ref_name)
            seen.add(ref_name)
    return tuple(refs)


def _parse_offset(raw_offset: bytes, *, ref_name: str) -> int:
    hours = int(raw_offset[1:3])
    minutes = int(raw_offset[3:5])
    if hours > 23 or minutes > 59:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an out-of-range UTC offset",
            ref_name=ref_name,
        )
    sign = 1 if raw_offset[:1] == b"+" else -1
    return sign * (hours * 3_600 + minutes * 60)


def _parse_epoch(raw_epoch: bytes, *, ref_name: str) -> int:
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an epoch that cannot be represented",
            ref_name=ref_name,
        ) from error
    if not _MIN_LOCALIZABLE_EPOCH <= epoch <= _MAX_LOCALIZABLE_EPOCH:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an epoch outside Workfold's localizable range",
            ref_name=ref_name,
        )
    return epoch


def _parse_identity(raw_identity: bytes, *, ref_name: str) -> tuple[bytes, bytes]:
    separator = raw_identity.rfind(b" <")
    if separator < 0 or not raw_identity.endswith(b">"):
        raise GitReflogParseError(
            "invalid_git_reflog_identity",
            "reflog entry has no valid name/email identity",
            ref_name=ref_name,
        )
    return raw_identity[:separator], raw_identity[separator + 2 : -1]


def _parse_reflog_line(raw_line: bytes, *, ref_name: str) -> _ParsedReflogLine:
    header, tab, raw_message = raw_line.partition(b"\t")
    if not tab:
        # update-ref permits an omitted message, represented by no tab at all
        # rather than by a trailing empty field.
        header = raw_line
        raw_message = b""
    old_raw, separator, remainder = header.partition(b" ")
    if not separator:
        raise GitReflogParseError(
            "invalid_git_reflog_entry",
            "reflog record has no old object ID",
            ref_name=ref_name,
        )
    new_raw, separator, signature_raw = remainder.partition(b" ")
    if not separator or _OID_RE.fullmatch(old_raw) is None or _OID_RE.fullmatch(new_raw) is None:
        raise GitReflogParseError(
            "invalid_git_reflog_object_id",
            "reflog record has an invalid old or new object ID",
            ref_name=ref_name,
        )
    if len(old_raw) != len(new_raw):
        raise GitReflogParseError(
            "invalid_git_reflog_object_id",
            "reflog old and new object IDs use different hash formats",
            ref_name=ref_name,
        )
    try:
        identity_raw, epoch_raw, offset_raw = signature_raw.rsplit(b" ", 2)
    except ValueError as error:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog identity does not end in an epoch and UTC offset",
            ref_name=ref_name,
        ) from error
    if _EPOCH_RE.fullmatch(epoch_raw) is None or _OFFSET_RE.fullmatch(offset_raw) is None:
        raise GitReflogParseError(
            "invalid_git_reflog_timestamp",
            "reflog entry has an invalid epoch or UTC offset",
            ref_name=ref_name,
        )
    name_raw, email_raw = _parse_identity(identity_raw, ref_name=ref_name)
    return _ParsedReflogLine(
        old_id=old_raw,
        new_id=new_raw,
        identity=identity_raw,
        epoch=epoch_raw,
        offset=offset_raw,
        actor_name=name_raw,
        actor_email=email_raw,
        message=raw_message,
        epoch_seconds=_parse_epoch(epoch_raw, ref_name=ref_name),
        offset_seconds=_parse_offset(offset_raw, ref_name=ref_name),
    )


def parse_reflog_entries(payload: bytes, *, ref_name: str) -> tuple[ParsedReflogEntry, ...]:
    """Parse a semantic reflog file from right-delimited identity/date fields.

    Reflog storage is ordered oldest to newest.  Returned entries are newest
    first and carry the corresponding ``ref@{N}`` selector at this snapshot.
    """

    if not payload:
        return ()
    if b"\0" in payload:
        raise GitReflogParseError(
            "invalid_git_reflog_entry",
            "reflog contains an impossible NUL byte",
            ref_name=ref_name,
        )
    if not payload.endswith(b"\n"):
        raise GitReflogParseError(
            "truncated_git_reflog_entry",
            "reflog ends inside a record",
            ref_name=ref_name,
        )
    raw_lines = payload[:-1].split(b"\n")
    raw_ref_name = os.fsencode(ref_name)
    parsed_oldest_first: list[ParsedReflogEntry] = []
    duplicate_counts: dict[tuple[bytes, bytes, bytes, bytes, bytes], int] = {}
    for index, raw_line in enumerate(raw_lines):
        parsed = _parse_reflog_line(raw_line, ref_name=ref_name)
        duplicate_key = parsed.duplicate_key
        duplicate_ordinal = duplicate_counts.get(duplicate_key, 0)
        duplicate_counts[duplicate_key] = duplicate_ordinal + 1
        parsed_oldest_first.append(
            parsed.to_entry(
                ref_name=ref_name,
                raw_ref_name=raw_ref_name,
                selector_index=len(raw_lines) - index - 1,
                duplicate_ordinal=duplicate_ordinal,
            )
        )
    return tuple(reversed(parsed_oldest_first))


def _decode_git_path(payload: bytes, *, repository: GitRepository) -> Path:
    if not payload.endswith(b"\n"):
        raise GitReflogReadError("invalid_git_reflog_path", "Git returned an invalid reflog path")
    raw_path = payload[:-1]
    if not raw_path or b"\0" in raw_path:
        raise GitReflogReadError("invalid_git_reflog_path", "Git returned an invalid reflog path")
    path = Path(os.fsdecode(raw_path))
    return path if path.is_absolute() else repository.root / path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _open_semantic_reflog(path: Path, *, repository: GitRepository) -> tuple[int, os.stat_result, Path]:
    """Open a repository-contained regular reflog without following its final link."""

    descriptor = -1
    try:
        resolved = path.resolve(strict=True)
        git_dir = repository.git_dir.resolve(strict=True)
        common_dir = repository.common_dir.resolve(strict=True)
        if not (_is_within(resolved, git_dir) or _is_within(resolved, common_dir)):
            raise GitReflogReadError(
                "unsafe_git_reflog_path",
                "Git resolved a reflog outside its repository metadata directories",
                path=path,
            )
        resolved_snapshot = os.lstat(resolved)
        if not stat.S_ISREG(resolved_snapshot.st_mode):
            raise GitReflogReadError(
                "invalid_git_reflog_file",
                "Git resolved a reflog that is not a regular file",
                path=resolved,
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(resolved, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GitReflogReadError(
                "invalid_git_reflog_file",
                "Git resolved a reflog that is not a regular file",
                path=resolved,
            )
        opened_descriptor = descriptor
        descriptor = -1
        return opened_descriptor, before, resolved
    except GitReflogReadError:
        raise
    except OSError as error:
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def read_semantic_reflog(path: Path, *, repository: GitRepository) -> tuple[bytes, bool]:
    """Read one regular reflog safely and report concurrent mutation."""

    descriptor, before, _resolved = _open_semantic_reflog(path, repository=repository)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read()
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, _snapshot_changed(before, after)


_DEFAULT_READ_SEMANTIC_REFLOG = read_semantic_reflog


def visit_semantic_reflog(
    path: Path,
    *,
    repository: GitRepository,
    ref_name: str,
    entry_consumer: Callable[[tuple[ParsedReflogEntry, ...]], None] | None = None,
    batch_size: int = 512,
) -> ReflogVisit:
    """Validate a complete reflog snapshot, then emit newest-first batches.

    Raw records and duplicate accounting live in an ephemeral SQLite spool.
    This keeps memory bounded without exposing a partially parsed reflog if a
    malformed record appears near the end of the file.
    """

    if batch_size < 1:
        raise ValueError("reflog batch_size must be positive")
    descriptor, before, _resolved = _open_semantic_reflog(path, repository=repository)
    record_count = 0
    has_nul = False
    truncated = False
    delivering_callbacks = False
    try:
        with tempfile.TemporaryDirectory(prefix="workfold-reflog-") as directory:
            connection = sqlite3.connect(f"{directory}/reflog.sqlite3")
            try:
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                connection.execute("PRAGMA temp_store=FILE")
                connection.execute("PRAGMA cache_size=-4096")
                connection.execute(
                    """
                    CREATE TABLE records (
                        ordinal INTEGER PRIMARY KEY,
                        raw_line BLOB NOT NULL,
                        duplicate_ordinal INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE duplicate_counts (
                        old_id BLOB NOT NULL,
                        new_id BLOB NOT NULL,
                        identity BLOB NOT NULL,
                        raw_timestamp BLOB NOT NULL,
                        message BLOB NOT NULL,
                        count INTEGER NOT NULL,
                        PRIMARY KEY (old_id, new_id, identity, raw_timestamp, message)
                    ) WITHOUT ROWID
                    """
                )
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    for raw_record in stream:
                        has_nul |= b"\0" in raw_record
                        if not raw_record.endswith(b"\n"):
                            truncated = True
                            continue
                        connection.execute(
                            "INSERT INTO records VALUES (?, ?, NULL)",
                            (record_count, raw_record[:-1]),
                        )
                        record_count += 1
                    after = os.fstat(stream.fileno())

                if has_nul:
                    raise GitReflogParseError(
                        "invalid_git_reflog_entry",
                        "reflog contains an impossible NUL byte",
                        ref_name=ref_name,
                        record_count=record_count,
                    )
                if truncated:
                    raise GitReflogParseError(
                        "truncated_git_reflog_entry",
                        "reflog ends inside a record",
                        ref_name=ref_name,
                        record_count=record_count,
                    )

                try:
                    for ordinal, raw_line in connection.execute(
                        "SELECT ordinal, raw_line FROM records ORDER BY ordinal"
                    ):
                        parsed = _parse_reflog_line(raw_line, ref_name=ref_name)
                        duplicate_key = parsed.duplicate_key
                        existing = connection.execute(
                            """
                            SELECT count FROM duplicate_counts
                             WHERE old_id = ? AND new_id = ? AND identity = ?
                               AND raw_timestamp = ? AND message = ?
                            """,
                            duplicate_key,
                        ).fetchone()
                        duplicate_ordinal = 0 if existing is None else int(existing[0])
                        if existing is None:
                            connection.execute(
                                "INSERT INTO duplicate_counts VALUES (?, ?, ?, ?, ?, 1)",
                                duplicate_key,
                            )
                        else:
                            connection.execute(
                                """
                                UPDATE duplicate_counts SET count = count + 1
                                 WHERE old_id = ? AND new_id = ? AND identity = ?
                                   AND raw_timestamp = ? AND message = ?
                                """,
                                duplicate_key,
                            )
                        connection.execute(
                            "UPDATE records SET duplicate_ordinal = ? WHERE ordinal = ?",
                            (duplicate_ordinal, ordinal),
                        )
                except GitReflogParseError as error:
                    raise GitReflogParseError(
                        error.code,
                        str(error),
                        ref_name=error.ref_name,
                        record_count=record_count,
                    ) from error
                connection.commit()

                raw_ref_name = os.fsencode(ref_name)
                captured = 0
                batch: list[ParsedReflogEntry] = []
                for ordinal, raw_line, duplicate_ordinal in connection.execute(
                    "SELECT ordinal, raw_line, duplicate_ordinal FROM records ORDER BY ordinal DESC"
                ):
                    parsed = _parse_reflog_line(raw_line, ref_name=ref_name)
                    batch.append(
                        parsed.to_entry(
                            ref_name=ref_name,
                            raw_ref_name=raw_ref_name,
                            selector_index=record_count - int(ordinal) - 1,
                            duplicate_ordinal=int(duplicate_ordinal),
                        )
                    )
                    if len(batch) >= batch_size:
                        if entry_consumer is not None:
                            delivering_callbacks = True
                            entry_consumer(tuple(batch))
                        captured += len(batch)
                        batch.clear()
                if batch:
                    if entry_consumer is not None:
                        delivering_callbacks = True
                        entry_consumer(tuple(batch))
                    captured += len(batch)
            finally:
                connection.close()
    except GitReflogParseError:
        raise
    except (OSError, sqlite3.Error) as error:
        if delivering_callbacks:
            raise
        raise GitReflogReadError(
            "git_reflog_read_error",
            f"semantic reflog could not be read: {error}",
            path=path,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return ReflogVisit(
        entry_count=record_count,
        captured_entry_count=captured,
        changed_during_read=_snapshot_changed(before, after),
    )


def _command_diagnostic(
    error: GitCommandError,
    *,
    repository: GitRepository,
    stage: str,
) -> CollectorDiagnostic:
    details = error.stderr_text
    message = str(error) if not details else f"{error}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage=stage,
        target=os.fspath(repository.root),
        path=os.fspath(repository.root),
        message=message,
        hint=error.hint,
    )


def _reflog_inventory_key(repository: GitRepository, ref_name: str) -> tuple[str, str]:
    """Return a key that separates worktree-local logs from shared logs."""

    is_worktree_local = not ref_name.startswith("refs/") or ref_name.startswith(_PER_WORKTREE_REF_PREFIXES)
    scope_identity = repository.context_identity if is_worktree_local else repository.identity
    return scope_identity, ref_name


def discover_reflog_names(runner: GitRunner, repository: GitRepository) -> tuple[str, ...]:
    """Use efficient reflog inventory when available, with an older-Git fallback."""

    try:
        payload = runner.run(("reflog", "list"), cwd=repository.root).stdout
    except GitCommandError:
        payload = runner.run(
            ("reflog", "show", "--all", "--format=%gD"),
            cwd=repository.root,
        ).stdout
        return parse_reflog_selectors(payload)
    return parse_reflog_list(payload)


class GitReflogCollector:
    """Enumerate and read local semantic reflogs without revision-walk loss."""

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._runner = runner or GitRunner()

    def collect(
        self,
        repositories: Sequence[GitRepository],
        *,
        entry_consumer: Callable[[tuple[CollectedGitReflog, ...]], None] | None = None,
        retain_entries: bool = True,
    ) -> GitReflogCollectionResult:
        """Collect reflogs independently of commit ref scope and identity filters."""

        collected: list[CollectedGitReflog] = []
        available_statuses: list[ReflogRef] = []
        unavailable_statuses: list[ReflogRef] = []
        diagnostics = DiagnosticBuffer()
        successful = 0
        discovered_ref_count = 0
        unavailable_entry_count = 0
        captured_entry_count = 0
        parse_errors = 0
        seen_inventory: set[tuple[str, str]] = set()

        for repository in repositories:
            repository_failed = False
            try:
                current_output = self._runner.run(
                    ("show-ref", "--head"),
                    cwd=repository.root,
                    allowed_returncodes=(0, 1),
                ).stdout
                current_refs = parse_current_refs(current_output)
                available_names = discover_reflog_names(self._runner, repository)
            except GitCommandError as error:
                diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_reflog_discovery"))
                continue
            except GitReflogParseError as error:
                parse_errors += 1
                diagnostics.append(
                    CollectorDiagnostic(
                        code=error.code,
                        stage="git_reflog_discovery",
                        target=os.fspath(repository.root),
                        provenance_id=error.ref_name,
                        message=str(error),
                    )
                )
                continue

            available_set = set(available_names)
            for ref_name in current_refs:
                if ref_name in available_set:
                    continue
                inventory_key = _reflog_inventory_key(repository, ref_name)
                if inventory_key in seen_inventory:
                    continue
                seen_inventory.add(inventory_key)
                discovered_ref_count += 1
                unavailable_statuses.append(ReflogRef(repository, ref_name, 0, 0))
            for ref_name in available_names:
                inventory_key = _reflog_inventory_key(repository, ref_name)
                if inventory_key in seen_inventory:
                    continue
                seen_inventory.add(inventory_key)
                discovered_ref_count += 1
                raw_record_count = 0
                captured_for_ref = 0
                try:
                    path_output = self._runner.run(
                        (
                            "rev-parse",
                            "--git-path",
                            f"logs/{ref_name}",
                        ),
                        cwd=repository.root,
                    ).stdout
                    reflog_path = _decode_git_path(path_output, repository=repository)

                    def consume_parsed(parsed_batch: tuple[ParsedReflogEntry, ...]) -> None:
                        nonlocal captured_for_ref, captured_entry_count
                        collected_batch = tuple(
                            CollectedGitReflog(repository=repository, entry=entry) for entry in parsed_batch
                        )
                        captured_for_ref += len(collected_batch)
                        captured_entry_count += len(collected_batch)
                        if retain_entries:
                            collected.extend(collected_batch)
                        if collected_batch and entry_consumer is not None:
                            entry_consumer(collected_batch)

                    if read_semantic_reflog is _DEFAULT_READ_SEMANTIC_REFLOG:
                        visit = visit_semantic_reflog(
                            reflog_path,
                            repository=repository,
                            ref_name=ref_name,
                            entry_consumer=consume_parsed,
                        )
                        raw_record_count = visit.entry_count
                        changed = visit.changed_during_read
                        if captured_for_ref != visit.captured_entry_count:
                            raise RuntimeError("reflog visitor capture accounting did not reconcile")
                    else:
                        # Preserve the public reader as a test/integration seam.
                        payload, changed = read_semantic_reflog(reflog_path, repository=repository)
                        raw_record_count = payload.count(b"\n")
                        parsed_entries = parse_reflog_entries(payload, ref_name=ref_name)
                        for start in range(0, len(parsed_entries), 512):
                            consume_parsed(parsed_entries[start : start + 512])
                except GitCommandError as error:
                    repository_failed = True
                    diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_reflog_path"))
                    available_statuses.append(ReflogRef(repository, ref_name, 0, 0))
                    continue
                except GitReflogReadError as error:
                    repository_failed = True
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_reflog_read",
                            target=os.fspath(repository.root),
                            path=os.fspath(error.path) if error.path is not None else None,
                            provenance_id=ref_name,
                            message=str(error),
                        )
                    )
                    available_statuses.append(ReflogRef(repository, ref_name, 0, 0))
                    continue
                except GitReflogParseError as error:
                    repository_failed = True
                    parse_errors += 1
                    raw_record_count = max(raw_record_count, error.record_count)
                    unavailable_entry_count += raw_record_count
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_reflog_parse",
                            target=os.fspath(repository.root),
                            provenance_id=error.ref_name,
                            message=str(error),
                        )
                    )
                    available_statuses.append(ReflogRef(repository, ref_name, raw_record_count, 0))
                    continue
                if changed:
                    repository_failed = True
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_reflog_changed_during_collection",
                            stage="git_reflog_read",
                            target=os.fspath(repository.root),
                            path=os.fspath(reflog_path),
                            provenance_id=ref_name,
                            message="The reflog changed while Workfold read it; the captured snapshot may be partial",
                        )
                    )
                available_statuses.append(ReflogRef(repository, ref_name, raw_record_count, captured_for_ref))
            if not repository_failed:
                successful += 1

        return GitReflogCollectionResult(
            entries=tuple(collected),
            available_refs=tuple(available_statuses),
            refs_without_reflog=tuple(unavailable_statuses),
            diagnostics=diagnostics.snapshot(),
            requested_repositories=len(repositories),
            successful_repositories=successful,
            discovered_refs=discovered_ref_count,
            captured_entries=captured_entry_count,
            unavailable_entries=unavailable_entry_count,
            parse_errors=parse_errors,
            records_retained=retain_entries,
        )


__all__ = [
    "CollectedGitReflog",
    "GitReflogCollectionResult",
    "GitReflogCollector",
    "GitReflogParseError",
    "GitReflogReadError",
    "ParsedReflogEntry",
    "ReflogVisit",
    "ReflogRef",
    "parse_current_refs",
    "parse_reflog_entries",
    "parse_reflog_list",
    "parse_reflog_selectors",
    "discover_reflog_names",
    "read_semantic_reflog",
    "visit_semantic_reflog",
]
