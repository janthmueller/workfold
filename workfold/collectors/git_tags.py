"""Local Git tag discovery and annotated tagger timestamp extraction."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from workfold.collectors.base import CollectorDiagnostic, CollectorResult
from workfold.collectors.git import GitCommandError, GitRepository, GitRunner, unique_semantic_repositories
from workfold.collectors.git_objects import (
    GitObjectParseError,
    GitSignature,
    parse_cat_file_batch,
    parse_git_signature,
)
from workfold.coverage import DiagnosticSeverity
from workfold.models import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.provenance import git_tag_id

_OID_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitTagParseError(ValueError):
    """A structured failure to parse tag discovery or a raw tag object."""

    def __init__(self, code: str, message: str, *, object_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.object_id = object_id


@dataclass(frozen=True, slots=True)
class DiscoveredGitTag:
    """One local tag ref as reported by ``for-each-ref``."""

    ref_name: str
    raw_ref_name: bytes
    object_id: str
    object_type: str

    @property
    def annotated(self) -> bool:
        """Whether the ref points at an independent tag object."""

        return self.object_type == "tag"


@dataclass(frozen=True, slots=True)
class ParsedTagObject:
    """Timestamp-relevant fields retained from one raw annotated tag object."""

    object_id: str
    target_id: str
    target_type: str
    tag_name: str
    raw_tag_name: bytes
    tagger: GitSignature | None
    subject: str
    raw_subject: bytes


@dataclass(frozen=True, slots=True)
class CollectedGitTag:
    """One local tag ref, including lightweight tag unavailability."""

    repository: GitRepository
    ref: DiscoveredGitTag
    target_id: str
    tagger: GitSignature | None
    subject: str | None = None

    @property
    def annotated(self) -> bool:
        return self.ref.annotated

    @property
    def tag_object_id(self) -> str | None:
        return self.ref.object_id if self.annotated else None

    def to_origin(self) -> RecordOrigin:
        """Create one record even when the tagger slot is unavailable."""

        return RecordOrigin(
            record_id=git_tag_id(
                self.repository.root,
                self.ref.ref_name,
                self.tag_object_id,
                self.target_id,
            ),
            source=Source.GIT,
            record_kind=RecordKind.ANNOTATED_TAG,
            repository_or_root=self.repository.root,
            object_id=self.tag_object_id,
            target_id=self.target_id,
            ref_name=self.ref.ref_name,
            author_name=self.tagger.identity.name if self.tagger is not None else None,
            author_email=self.tagger.identity.email if self.tagger is not None else None,
            description=self.subject,
        )

    def to_observation(self) -> TimestampObservation:
        """Return the independent tagger timestamp for an annotated tag."""

        if self.tagger is None:
            raise ValueError("this tag has no independent tagger timestamp")
        return TimestampObservation.create(
            self.to_origin(),
            TimestampKind.GIT_TAGGER,
            self.tagger.epoch_nanoseconds,
            self.tagger.raw_timestamp,
            original_offset_minutes=self.tagger.offset_seconds // 60,
            actor_name=self.tagger.identity.name,
            actor_email=self.tagger.identity.email,
        )


@dataclass(frozen=True, slots=True)
class GitTagRepositoryAccounting:
    """Reconciled tag and tagger-slot counters for one repository."""

    repository: GitRepository
    discovered_tags: int
    captured_tags: int
    record_errors: int
    annotated_tags: int
    lightweight_tags: int
    captured_tagger_timestamps: int
    unavailable_tagger_timestamps: int
    unavailable_objects: int
    parse_errors: int
    operational_errors: int
    successful: bool

    def __post_init__(self) -> None:
        counters = (
            self.discovered_tags,
            self.captured_tags,
            self.record_errors,
            self.annotated_tags,
            self.lightweight_tags,
            self.captured_tagger_timestamps,
            self.unavailable_tagger_timestamps,
            self.unavailable_objects,
            self.parse_errors,
            self.operational_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git tag repository counters must be non-negative")
        if self.discovered_tags != self.captured_tags + self.record_errors:
            raise ValueError("Git tag repository record accounting does not reconcile")
        if self.discovered_tags != self.annotated_tags + self.lightweight_tags:
            raise ValueError("Git tag repository discovery accounting does not reconcile")
        if self.captured_tags != self.captured_tagger_timestamps + self.unavailable_tagger_timestamps:
            raise ValueError("Git tag repository timestamp accounting does not reconcile")

    @property
    def repository_root(self) -> Path:
        """Filesystem root used as the repository coverage target."""

        return self.repository.root

    @property
    def repository_identity(self) -> str:
        """Canonical repository identity used for collection deduplication."""

        return self.repository.identity

    @property
    def eligible_tags(self) -> int:
        """Tag records eligible for a tagger timestamp slot."""

        return self.captured_tags


@dataclass(frozen=True, slots=True)
class GitTagCollectionResult:
    """Tag records plus explicit tagger availability accounting."""

    tags: tuple[CollectedGitTag, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_repositories: int
    successful_repositories: int
    discovered_tags: int
    annotated_tags: int
    lightweight_tags: int
    captured_tagger_timestamps: int
    unavailable_tagger_timestamps: int
    unavailable_objects: int
    parse_errors: int
    repository_accounting: tuple[GitTagRepositoryAccounting, ...] = ()
    records_retained: bool = True

    def __post_init__(self) -> None:
        captured_tags = sum(item.captured_tags for item in self.repository_accounting)
        if self.repository_accounting:
            if self.records_retained and len(self.tags) != captured_tags:
                raise ValueError("retained Git tags do not match repository accounting")
            if len(self.tags) > captured_tags:
                raise ValueError("retained Git tags exceed captured repository accounting")

    @property
    def is_partial(self) -> bool:
        return bool(self.diagnostics)

    def to_domain_result(self) -> CollectorResult[RecordOrigin, TimestampObservation]:
        """Expose every tag origin and only real independent tagger dates."""

        return CollectorResult(
            origins=tuple(item.to_origin() for item in self.tags),
            observations=tuple(item.to_observation() for item in self.tags if item.tagger is not None),
            diagnostics=self.diagnostics,
        )


def parse_tag_refs(payload: bytes) -> tuple[DiscoveredGitTag, ...]:
    """Parse NUL-delimited fields from ``for-each-ref`` tag output."""

    if not payload:
        return ()
    records: list[DiscoveredGitTag] = []
    for raw_line in payload.splitlines():
        fields = raw_line.split(b"\0")
        if len(fields) != 4 or fields[-1] != b"":
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned malformed tag fields")
        ref_raw, object_raw, type_raw, _terminator = fields
        if not ref_raw.startswith(b"refs/tags/") or len(ref_raw) == len(b"refs/tags/"):
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag ref")
        if _OID_RE.fullmatch(object_raw) is None:
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag object ID")
        if type_raw not in {b"blob", b"commit", b"tag", b"tree"}:
            raise GitTagParseError("invalid_git_tag_ref", "for-each-ref returned an invalid tag object type")
        records.append(
            DiscoveredGitTag(
                ref_name=ref_raw.decode("utf-8", errors="surrogateescape"),
                raw_ref_name=ref_raw,
                object_id=object_raw.decode("ascii"),
                object_type=type_raw.decode("ascii"),
            )
        )
    return tuple(records)


def parse_tag_object(object_id: str, data: bytes) -> ParsedTagObject:
    """Parse exact target, optional tagger signature and subject from a tag object."""

    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None:
        raise GitTagParseError("invalid_tag_object_id", "invalid tag object ID", object_id=object_id)
    header_bytes, separator, message = data.partition(b"\n\n")
    if not separator:
        raise GitTagParseError(
            "invalid_tag_object",
            "tag object has no header/message boundary",
            object_id=object_id,
        )
    headers: dict[bytes, bytes] = {}
    current_name: bytes | None = None
    for line in header_bytes.split(b"\n"):
        if line.startswith(b" "):
            if current_name is None:
                raise GitTagParseError(
                    "invalid_tag_header",
                    "orphaned continuation line in tag header",
                    object_id=object_id,
                )
            continue
        name, field_separator, value = line.partition(b" ")
        if not field_separator or not name:
            raise GitTagParseError(
                "invalid_tag_header",
                "malformed tag header line",
                object_id=object_id,
            )
        current_name = name
        if name in {b"object", b"type", b"tag", b"tagger"}:
            if name in headers:
                raise GitTagParseError(
                    "invalid_tag_header",
                    f"duplicate {name.decode('ascii')} header",
                    object_id=object_id,
                )
            headers[name] = value

    missing = [name for name in (b"object", b"type", b"tag") if name not in headers]
    if missing:
        names = ", ".join(name.decode("ascii") for name in missing)
        raise GitTagParseError(
            "invalid_tag_header",
            f"tag object is missing required header(s): {names}",
            object_id=object_id,
        )
    target_raw = headers[b"object"]
    if _OID_RE.fullmatch(target_raw) is None:
        raise GitTagParseError(
            "invalid_tag_header",
            "tag object has an invalid target object ID",
            object_id=object_id,
        )
    type_raw = headers[b"type"]
    if type_raw not in {b"blob", b"commit", b"tag", b"tree"}:
        raise GitTagParseError(
            "invalid_tag_header",
            "tag object has an invalid target object type",
            object_id=object_id,
        )
    tagger_raw = headers.get(b"tagger")
    try:
        tagger = parse_git_signature(tagger_raw, role="tagger", object_id=object_id) if tagger_raw is not None else None
    except GitObjectParseError as error:
        raise GitTagParseError(error.code, str(error), object_id=object_id) from error
    raw_subject = message.split(b"\n", 1)[0]
    return ParsedTagObject(
        object_id=object_id,
        target_id=target_raw.decode("ascii"),
        target_type=type_raw.decode("ascii"),
        tag_name=headers[b"tag"].decode("utf-8", errors="surrogateescape"),
        raw_tag_name=headers[b"tag"],
        tagger=tagger,
        subject=raw_subject.decode("utf-8", errors="surrogateescape"),
        raw_subject=raw_subject,
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


class GitTagCollector:
    """Collect local lightweight and annotated tags without traversing commits."""

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._runner = runner or GitRunner()

    def collect(
        self,
        repositories: Sequence[GitRepository],
        *,
        tag_consumer: Callable[[tuple[CollectedGitTag, ...]], None] | None = None,
        retain_tags: bool = True,
    ) -> GitTagCollectionResult:
        """Collect all local tag refs independently of commit reachability scope."""

        semantic_repositories = unique_semantic_repositories(repositories)
        tags: list[CollectedGitTag] = []
        diagnostics: list[CollectorDiagnostic] = []
        repository_accounting: list[GitTagRepositoryAccounting] = []

        for repository in semantic_repositories:
            diagnostic_start = len(diagnostics)
            discovered_for_repository = 0
            captured_tags_for_repository = 0
            annotated_for_repository = 0
            lightweight_for_repository = 0
            captured_timestamps_for_repository = 0
            unavailable_timestamps_for_repository = 0
            unavailable_objects_for_repository = 0
            parse_errors_for_repository = 0
            successful_for_repository = False
            try:
                try:
                    output = self._runner.run(
                        (
                            "for-each-ref",
                            "--format=%(refname)%00%(objectname)%00%(objecttype)%00",
                            "refs/tags/",
                        ),
                        cwd=repository.root,
                    ).stdout
                    refs = parse_tag_refs(output)
                except GitCommandError as error:
                    diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_tag_discovery"))
                    continue
                except GitTagParseError as error:
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_tag_discovery",
                            target=os.fspath(repository.root),
                            message=str(error),
                        )
                    )
                    parse_errors_for_repository += 1
                    continue

                discovered_for_repository = len(refs)
                annotated_refs = [item for item in refs if item.annotated]
                lightweight_refs = [item for item in refs if not item.annotated]
                annotated_for_repository = len(annotated_refs)
                lightweight_for_repository = len(lightweight_refs)
                captured_tags_for_repository += len(lightweight_refs)
                unavailable_timestamps_for_repository += len(lightweight_refs)
                lightweight_batch = tuple(
                    CollectedGitTag(
                        repository=repository,
                        ref=item,
                        target_id=item.object_id,
                        tagger=None,
                    )
                    for item in lightweight_refs
                )
                if retain_tags:
                    tags.extend(lightweight_batch)
                if lightweight_batch and tag_consumer is not None:
                    tag_consumer(lightweight_batch)
                if not annotated_refs:
                    successful_for_repository = True
                    continue

                object_ids = tuple(dict.fromkeys(item.object_id for item in annotated_refs))
                try:
                    batch_output = self._runner.run(
                        ("cat-file", "--batch"),
                        cwd=repository.root,
                        input_data=b"".join(value.encode("ascii") + b"\n" for value in object_ids),
                    ).stdout
                    batch = parse_cat_file_batch(batch_output, object_ids)
                except GitCommandError as error:
                    diagnostics.append(_command_diagnostic(error, repository=repository, stage="git_tag_object_read"))
                    parse_errors_for_repository += len(annotated_refs)
                    continue
                except GitObjectParseError as error:
                    diagnostics.append(
                        CollectorDiagnostic(
                            code=error.code,
                            stage="git_tag_object_read",
                            target=os.fspath(repository.root),
                            provenance_id=error.object_id,
                            message=str(error),
                        )
                    )
                    parse_errors_for_repository += len(annotated_refs)
                    continue

                refs_by_object: dict[str, list[DiscoveredGitTag]] = {}
                for ref in annotated_refs:
                    refs_by_object.setdefault(ref.object_id, []).append(ref)
                for missing in batch.unavailable:
                    affected = refs_by_object[missing.requested_id]
                    unavailable_objects_for_repository += 1
                    parse_errors_for_repository += len(affected)
                    diagnostics.append(
                        CollectorDiagnostic(
                            code="git_tag_object_unavailable",
                            stage="git_tag_object_read",
                            target=os.fspath(repository.root),
                            provenance_id=missing.requested_id,
                            message=f"Git tag object is unavailable ({missing.reason})",
                            hint="Workfold will not fetch missing objects.",
                        )
                    )
                for batch_object in batch.objects:
                    affected = refs_by_object[batch_object.object_id]
                    if batch_object.object_type != "tag":
                        parse_errors_for_repository += len(affected)
                        diagnostics.append(
                            CollectorDiagnostic(
                                code="git_object_not_tag",
                                stage="git_tag_object_parse",
                                target=os.fspath(repository.root),
                                provenance_id=batch_object.object_id,
                                message=f"tag ref object has unexpected type {batch_object.object_type!r}",
                            )
                        )
                        continue
                    try:
                        parsed = parse_tag_object(batch_object.object_id, batch_object.data)
                    except GitTagParseError as error:
                        parse_errors_for_repository += len(affected)
                        diagnostics.append(
                            CollectorDiagnostic(
                                code=error.code,
                                stage="git_tag_object_parse",
                                target=os.fspath(repository.root),
                                provenance_id=error.object_id,
                                message=str(error),
                            )
                        )
                        continue
                    captured_tags_for_repository += len(affected)
                    if parsed.tagger is None:
                        unavailable_timestamps_for_repository += len(affected)
                    else:
                        captured_timestamps_for_repository += len(affected)
                    collected_batch = tuple(
                        CollectedGitTag(
                            repository=repository,
                            ref=ref,
                            target_id=parsed.target_id,
                            tagger=parsed.tagger,
                            subject=parsed.subject,
                        )
                        for ref in affected
                    )
                    if retain_tags:
                        tags.extend(collected_batch)
                    if collected_batch and tag_consumer is not None:
                        tag_consumer(collected_batch)
                successful_for_repository = True
            finally:
                operational_errors = sum(
                    item.severity is DiagnosticSeverity.ERROR for item in diagnostics[diagnostic_start:]
                )
                repository_accounting.append(
                    GitTagRepositoryAccounting(
                        repository=repository,
                        discovered_tags=discovered_for_repository,
                        captured_tags=captured_tags_for_repository,
                        record_errors=discovered_for_repository - captured_tags_for_repository,
                        annotated_tags=annotated_for_repository,
                        lightweight_tags=lightweight_for_repository,
                        captured_tagger_timestamps=captured_timestamps_for_repository,
                        unavailable_tagger_timestamps=unavailable_timestamps_for_repository,
                        unavailable_objects=unavailable_objects_for_repository,
                        parse_errors=parse_errors_for_repository,
                        operational_errors=operational_errors,
                        successful=successful_for_repository,
                    )
                )

        successful = sum(item.successful for item in repository_accounting)
        discovered = sum(item.discovered_tags for item in repository_accounting)
        annotated = sum(item.annotated_tags for item in repository_accounting)
        lightweight = sum(item.lightweight_tags for item in repository_accounting)
        captured = sum(item.captured_tagger_timestamps for item in repository_accounting)
        unavailable_tagger = sum(item.unavailable_tagger_timestamps for item in repository_accounting)
        unavailable_objects = sum(item.unavailable_objects for item in repository_accounting)
        parse_errors = sum(item.parse_errors for item in repository_accounting)

        return GitTagCollectionResult(
            tags=tuple(tags),
            diagnostics=tuple(diagnostics),
            requested_repositories=len(semantic_repositories),
            successful_repositories=successful,
            discovered_tags=discovered,
            annotated_tags=annotated,
            lightweight_tags=lightweight,
            captured_tagger_timestamps=captured,
            unavailable_tagger_timestamps=unavailable_tagger,
            unavailable_objects=unavailable_objects,
            parse_errors=parse_errors,
            repository_accounting=tuple(repository_accounting),
            records_retained=retain_tags,
        )


__all__ = [
    "CollectedGitTag",
    "DiscoveredGitTag",
    "GitTagCollectionResult",
    "GitTagCollector",
    "GitTagParseError",
    "GitTagRepositoryAccounting",
    "ParsedTagObject",
    "parse_tag_object",
    "parse_tag_refs",
]
