"""Read and normalize bounded batches of local Git tag objects."""

from __future__ import annotations

import os
from dataclasses import dataclass

from workfold.collectors.base import CollectorDiagnostic, DiagnosticBuffer
from workfold.collectors.git import GitCommandError, GitRepository, GitRunner
from workfold.collectors.git_objects import GitObjectParseError, parse_cat_file_batch
from workfold.collectors.tags.models import CollectedGitTag, DiscoveredGitTag
from workfold.collectors.tags.parser import GitTagParseError, parse_tag_object


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
    )


@dataclass(frozen=True, slots=True)
class TagBatchOutcome:
    """Records and accounting extracted from one bounded ref batch."""

    collected: tuple[CollectedGitTag, ...]
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
) -> TagBatchOutcome:
    """Collect lightweight refs and read annotated tag objects in one batch."""

    lightweight_refs = tuple(item for item in refs if not item.annotated)
    annotated_refs = tuple(item for item in refs if item.annotated)
    collected: list[CollectedGitTag] = [
        CollectedGitTag(repository, item, item.object_id, None) for item in lightweight_refs
    ]
    captured_tags = len(lightweight_refs)
    captured_timestamps = 0
    unavailable_timestamps = len(lightweight_refs)
    unavailable_objects = 0
    parse_errors = 0
    if not annotated_refs:
        return TagBatchOutcome(
            tuple(collected),
            captured_tags,
            captured_timestamps,
            unavailable_timestamps,
            unavailable_objects,
            parse_errors,
        )

    object_ids = tuple(dict.fromkeys(item.object_id for item in annotated_refs))
    try:
        batch_output = runner.run(
            ("cat-file", "--batch"),
            cwd=repository.root,
            input_data=b"".join(value.encode("ascii") + b"\n" for value in object_ids),
        ).stdout
        batch = parse_cat_file_batch(batch_output, object_ids)
    except GitCommandError as error:
        diagnostics.append(command_diagnostic(error, repository=repository, stage="git_tag_object_read"))
        return TagBatchOutcome(
            tuple(collected),
            captured_tags,
            captured_timestamps,
            unavailable_timestamps,
            unavailable_objects,
            len(annotated_refs),
            True,
        )
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
        return TagBatchOutcome(
            tuple(collected),
            captured_tags,
            captured_timestamps,
            unavailable_timestamps,
            unavailable_objects,
            len(annotated_refs),
            True,
        )

    refs_by_object: dict[str, list[DiscoveredGitTag]] = {}
    for ref in annotated_refs:
        refs_by_object.setdefault(ref.object_id, []).append(ref)
    for missing in batch.unavailable:
        affected = refs_by_object[missing.requested_id]
        unavailable_objects += 1
        parse_errors += len(affected)
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
            parse_errors += len(affected)
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
        captured_tags += len(affected)
        if parsed.tagger is None:
            unavailable_timestamps += len(affected)
        else:
            captured_timestamps += len(affected)
        collected.extend(
            CollectedGitTag(
                repository=repository,
                ref=ref,
                target_id=parsed.target_id,
                tagger=parsed.tagger,
                subject=parsed.subject,
            )
            for ref in affected
        )
    return TagBatchOutcome(
        tuple(collected),
        captured_tags,
        captured_timestamps,
        unavailable_timestamps,
        unavailable_objects,
        parse_errors,
    )
