from __future__ import annotations

from typing import cast

import pytest
from workfold.application.collection_plan import CollectionPlan
from workfold.domain.evidence import (
    EvidenceKind,
    EvidenceSelection,
    evidence_kinds_from_mask,
    evidence_mask,
    evidence_mask_source,
    expand_evidence_selectors,
    supported_marker_evidence_masks,
)
from workfold.domain.observations import EntryType, RecordKind, Source, TimestampKind


def test_evidence_catalog_round_trips_normalized_dimensions() -> None:
    for kind in EvidenceKind:
        assert EvidenceKind.from_dimensions(kind.record_kind, kind.timestamp_kind, kind.entry_type) is kind
        assert kind.timestamp_kind.source is kind.source


def test_compact_marker_signatures_round_trip_every_supported_shape() -> None:
    supported = supported_marker_evidence_masks()

    assert len(supported) == len(EvidenceKind) + 2
    assert all(evidence_mask(evidence_kinds_from_mask(mask)) == mask for mask in supported)
    assert evidence_kinds_from_mask(supported[-2]) == (
        EvidenceKind.GIT_COMMIT_AUTHOR,
        EvidenceKind.GIT_COMMIT_COMMITTER,
    )


def test_evidence_masks_reject_empty_unknown_and_mixed_source_signatures() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        evidence_mask(())
    with pytest.raises(TypeError, match="integer"):
        evidence_kinds_from_mask(True)
    with pytest.raises(ValueError, match="supported event kinds"):
        evidence_kinds_from_mask(1 << len(EvidenceKind))
    with pytest.raises(ValueError, match="one source"):
        evidence_mask_source(evidence_mask((EvidenceKind.GIT_COMMIT_AUTHOR, EvidenceKind.FS_FILE_MODIFIED)))


def test_selector_expansion_is_case_insensitive_deduplicated_and_canonical() -> None:
    selection = expand_evidence_selectors(
        ("GIT:*:COMMITTER", "git:tag:tagger", "git:commit:committer"),
        option="--events",
    )

    assert selection.kinds == (
        EvidenceKind.GIT_COMMIT_COMMITTER,
        EvidenceKind.GIT_FILE_CHANGE_COMMITTER,
        EvidenceKind.GIT_TAG_TAGGER,
    )


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("git:*", tuple(kind for kind in EvidenceKind if kind.source is Source.GIT)),
        ("git:commit:*", (EvidenceKind.GIT_COMMIT_AUTHOR, EvidenceKind.GIT_COMMIT_COMMITTER)),
        (
            "git:*:author",
            (EvidenceKind.GIT_COMMIT_AUTHOR, EvidenceKind.GIT_FILE_CHANGE_AUTHOR),
        ),
        ("fs:*", tuple(kind for kind in EvidenceKind if kind.source is Source.FILESYSTEM)),
        ("*", tuple(EvidenceKind)),
    ],
)
def test_documented_wildcards_expand_against_the_catalog(
    selector: str,
    expected: tuple[EvidenceKind, ...],
) -> None:
    assert expand_evidence_selectors((selector,), option="--events").kinds == expected


def test_selector_errors_include_semantic_migration_suggestions() -> None:
    with pytest.raises(ValueError, match="fs:file:birth"):
        expand_evidence_selectors(("fs:created",), option="--events")
    with pytest.raises(ValueError, match=r"git:\*:committer"):
        expand_evidence_selectors(("git:committer:*",), option="--events")


def test_collection_plan_keeps_commit_and_file_change_roles_independent() -> None:
    selection = EvidenceSelection.create(
        (
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.GIT_FILE_CHANGE_COMMITTER,
            EvidenceKind.GIT_TAG_TAGGER,
            EvidenceKind.FS_FILE_ACCESSED,
        )
    )

    plan = CollectionPlan.from_selection(selection)

    assert plan.commit_timestamps == (TimestampKind.GIT_AUTHOR,)
    assert plan.file_change_timestamps == (TimestampKind.GIT_COMMITTER,)
    assert plan.commit_scan_timestamps == (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER)
    assert plan.collect_tags
    assert not plan.collect_reflogs
    assert plan.filesystem_timestamps == ((EntryType.REGULAR_FILE, (TimestampKind.FS_ACCESSED,)),)
    assert plan.includes_source(Source.GIT)
    assert plan.includes_source(Source.FILESYSTEM)


def test_unsupported_record_timestamp_pair_has_no_public_selector() -> None:
    with pytest.raises(ValueError, match="unsupported evidence dimensions"):
        EvidenceKind.from_dimensions(RecordKind.TAG, TimestampKind.GIT_AUTHOR)


def test_evidence_selection_rejects_string_values_even_when_they_compare_equal() -> None:
    raw = cast(tuple[EvidenceKind, ...], ("git:tag:tagger",))

    with pytest.raises(TypeError, match="EvidenceKind"):
        EvidenceSelection(raw)
