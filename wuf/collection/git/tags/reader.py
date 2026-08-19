"""Read and normalize bounded batches of local Git tag objects."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from wuf.collection.diagnostics import CollectorDiagnostic, DiagnosticBuffer, DiagnosticSeverity
from wuf.collection.git.command_error import GitCommandError
from wuf.collection.git.objects.cat_file import parse_cat_file_batch
from wuf.collection.git.objects.compact import read_cat_file_batch_compact_object
from wuf.collection.git.objects.models import (
    CompactBatchObject,
    CompactBatchResult,
    GitObjectParseError,
    InvalidBatchObject,
    UnavailableBatchObject,
    UnexpectedBatchObject,
)
from wuf.collection.git.repository import GitRepository
from wuf.collection.git.runner import GitRunner
from wuf.collection.git.tags.models import CollectedGitTag, DiscoveredGitTag
from wuf.collection.git.tags.parser import GitTagParseError, parse_tag_object


class _GitTagObjectReadError(RuntimeError):
    """Local I/O failure while preparing or consuming a tag object stream."""


def command_diagnostic(
    error: GitCommandError,
    *,
    repository: GitRepository,
    stage: str,
) -> CollectorDiagnostic:
    """Translate a failed Git command into a collector diagnostic."""

    details = error.stderr_text
    message = str(error) if not details else f"{error}: {details}"
    return CollectorDiagnostic(
        code=error.code,
        stage=stage,
        target=os.fspath(repository.root),
        path=os.fspath(repository.root),
        message=message,
        hint=error.hint,
        category=error.category,
    )


@dataclass(frozen=True, slots=True)
class TagBatchOutcome:
    """Records and accounting extracted from one bounded ref batch."""

    captured_tags: int
    captured_timestamps: int
    unavailable_timestamps: int
    unavailable_objects: int
    parse_errors: int
    failed: bool = False


def collect_tag_batch(
    repository: GitRepository,
    refs: tuple[DiscoveredGitTag, ...],
    runner: GitRunner,
    diagnostics: DiagnosticBuffer,
    record_consumer: Callable[[CollectedGitTag], None],
) -> TagBatchOutcome:
    """Collect lightweight refs and read annotated tag objects in one batch."""

    lightweight_refs = tuple(item for item in refs if not item.annotated)
    annotated_refs = tuple(item for item in refs if item.annotated)
    for item in lightweight_refs:
        record_consumer(CollectedGitTag(repository, item, item.object_id, None))
    captured_tags = len(lightweight_refs)
    captured_timestamps = 0
    unavailable_timestamps = len(lightweight_refs)
    unavailable_objects = 0
    parse_errors = 0
    if not annotated_refs:
        return TagBatchOutcome(
            captured_tags,
            captured_timestamps,
            unavailable_timestamps,
            unavailable_objects,
            parse_errors,
        )

    object_ids = tuple(dict.fromkeys(item.object_id for item in annotated_refs))
    refs_by_object: dict[str, list[DiscoveredGitTag]] = {}
    for ref in annotated_refs:
        refs_by_object.setdefault(ref.object_id, []).append(ref)
    processed_object_ids: set[str] = set()
    failed = False
    try:
        for result in _iter_tag_objects(repository, object_ids, runner):
            object_id = result.requested_id if isinstance(result, UnavailableBatchObject) else result.object_id
            processed_object_ids.add(object_id)
            affected = refs_by_object[object_id]
            if isinstance(result, UnavailableBatchObject):
                unavailable_objects += 1
                parse_errors += len(affected)
                diagnostics.append(
                    CollectorDiagnostic(
                        code="git_tag_object_unavailable",
                        stage="git_tag_object_read",
                        target=os.fspath(repository.root),
                        provenance_id=result.requested_id,
                        message=f"Git tag object is unavailable ({result.reason})",
                        hint="Wuf will not fetch missing objects.",
                    )
                )
                continue
            if isinstance(result, UnexpectedBatchObject):
                parse_errors += len(affected)
                diagnostics.append(
                    CollectorDiagnostic(
                        code="git_object_not_tag",
                        stage="git_tag_object_parse",
                        target=os.fspath(repository.root),
                        provenance_id=result.object_id,
                        message=f"tag ref object has unexpected type {result.object_type!r}",
                    )
                )
                continue
            if isinstance(result, InvalidBatchObject):
                parse_errors += len(affected)
                diagnostics.append(
                    CollectorDiagnostic(
                        code=result.code,
                        stage="git_tag_object_parse",
                        target=os.fspath(repository.root),
                        provenance_id=result.object_id,
                        message=result.message,
                    )
                )
                continue
            try:
                parsed = parse_tag_object(result.object_id, result.data)
            except GitTagParseError as error:
                parse_errors += len(affected)
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
            subject = parsed.subject
            if result.subject_truncated:
                subject = f"{subject}…"
                diagnostics.append(
                    CollectorDiagnostic(
                        code="git_tag_subject_truncated",
                        stage="git_tag_object_parse",
                        target=os.fspath(repository.root),
                        provenance_id=result.object_id,
                        message=(
                            "tag subject exceeds the retained metadata limit; "
                            "the tagger timestamp and identity were preserved"
                        ),
                        severity=DiagnosticSeverity.WARNING,
                    )
                )
            captured_tags += len(affected)
            if parsed.tagger is None:
                unavailable_timestamps += len(affected)
            else:
                captured_timestamps += len(affected)
            for ref in affected:
                record_consumer(
                    CollectedGitTag(
                        repository=repository,
                        ref=ref,
                        target_id=parsed.target_id,
                        tagger=parsed.tagger,
                        subject=subject,
                    )
                )
    except GitCommandError as error:
        diagnostics.append(command_diagnostic(error, repository=repository, stage="git_tag_object_read"))
        failed = True
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
        failed = True
    except _GitTagObjectReadError as error:
        diagnostics.append(
            CollectorDiagnostic(
                code="git_tag_object_io_error",
                stage="git_tag_object_read",
                target=os.fspath(repository.root),
                message=str(error),
            )
        )
        failed = True
    if failed:
        parse_errors += sum(
            len(refs_by_object[object_id]) for object_id in object_ids if object_id not in processed_object_ids
        )
    return TagBatchOutcome(
        captured_tags,
        captured_timestamps,
        unavailable_timestamps,
        unavailable_objects,
        parse_errors,
        failed,
    )


def _iter_tag_objects(
    repository: GitRepository,
    object_ids: tuple[str, ...],
    runner: GitRunner,
) -> Iterator[CompactBatchResult]:
    try:
        if not runner.streams_subprocess_output:
            # Test/custom runner adapters cannot expose a live pipe. Keep their
            # compatibility path bounded by the configured tag-ref batch.
            batch_output = runner.run(
                ("cat-file", "--batch"),
                cwd=repository.root,
                input_data=b"".join(value.encode("ascii") + b"\n" for value in object_ids),
            ).stdout
            batch = parse_cat_file_batch(batch_output, object_ids)
            for missing in batch.unavailable:
                yield missing
            for item in batch.objects:
                if item.object_type != "tag":
                    yield UnexpectedBatchObject(item.object_id, item.object_type)
                else:
                    yield CompactBatchObject(item.object_id, item.object_type, item.data)
            return

        with tempfile.TemporaryFile() as requests:
            for object_id in object_ids:
                requests.write(object_id.encode("ascii") + b"\n")
            requests.seek(0)
            with runner.open_stdout(
                ("cat-file", "--batch=%(objectname) %(objecttype) %(objectsize)"),
                cwd=repository.root,
                input_stream=requests,
            ) as stdout:
                for object_id in object_ids:
                    yield read_cat_file_batch_compact_object(
                        stdout,
                        expected_object_id=object_id,
                        expected_object_type="tag",
                        retained_header_names=(b"object", b"type", b"tag", b"tagger"),
                        object_label="tag",
                    )
                if stdout.read(1):
                    raise GitObjectParseError(
                        "unexpected_cat_file_output",
                        "cat-file batch returned trailing unrequested output",
                    )
    except OSError as error:
        raise _GitTagObjectReadError(f"could not prepare or read the Git tag object stream: {error}") from error
