"""Compact coverage status for normal terminal output."""

from __future__ import annotations

from collections.abc import Mapping

from wuf.application.report import CompletenessAssessment
from wuf.domain.observations import TimestampKind
from wuf.reporting.terminal.coverage_scope import pruned_ignored_subtree_label, timestamp_label

COMPLETE_COVERAGE_STATUS = "complete for all discoverable timestamps in the requested scope"


def coverage_status_label(
    assessment: CompletenessAssessment,
) -> str:
    """Summarize whether enabled collectors completed their requested scope."""

    if assessment.is_partial:
        reasons: list[str] = []
        if assessment.filesystem_inventory_failures:
            reasons.append(
                "filesystem inventory incomplete"
                if assessment.filesystem_inventory_failures == 1
                else f"{assessment.filesystem_inventory_failures:,} filesystem inventories incomplete"
            )
        if assessment.collection_errors:
            reasons.append(_counted(assessment.collection_errors, "collection error"))
        if assessment.collection_warnings:
            reasons.append(_counted(assessment.collection_warnings, "collection warning"))
        if not reasons:
            reasons.append("collection incomplete")
        label = "partial · " + " · ".join(reasons)
    else:
        label = COMPLETE_COVERAGE_STATUS
    qualifiers: list[str] = []
    if assessment.git_identity_scope_active:
        qualifiers.append("Git identity scope active")
    if assessment.explicit_exclusions_active:
        qualifiers.append("explicit exclusions active")
    pruned_ignored_subtrees = assessment.pruned_ignored_subtrees
    if pruned_ignored_subtrees:
        qualifiers.append(pruned_ignored_subtree_label(pruned_ignored_subtrees))
    for capability in assessment.capability_limitations:
        qualifier = f"{capability.name} unavailable"
        if capability.total_targets > 1:
            target_scope = (
                f"all {capability.total_targets:,} targets"
                if capability.affected_targets == capability.total_targets
                else f"{capability.affected_targets:,} of {capability.total_targets:,} targets"
            )
            qualifier += f" on {target_scope}"
        if capability.notes:
            qualifier += ": " + "; ".join(capability.notes)
        qualifiers.append(qualifier)
    unavailable_by_kind = dict(assessment.unavailable_timestamps)
    if unavailable_by_kind:
        qualifiers.append(_unavailable_timestamp_label(unavailable_by_kind))
    if qualifiers:
        label += "; " + "; ".join(qualifiers)
    return label


def _counted(count: int, singular: str) -> str:
    return f"{count:,} {singular if count == 1 else singular + 's'}"


def _unavailable_timestamp_label(counts: Mapping[TimestampKind, int]) -> str:
    total = sum(counts.values())
    if len(counts) == 1:
        kind, count = next(iter(counts.items()))
        noun = "timestamp" if count == 1 else "timestamps"
        record_noun = "source record" if count == 1 else "source records"
        return f"{count:,} {timestamp_label(kind)} {noun} unavailable on {record_noun}"
    details = ", ".join(
        f"{count:,} {timestamp_label(kind)}" for kind, count in sorted(counts.items(), key=lambda item: item[0].value)
    )
    noun = "timestamp slot" if total == 1 else "timestamp slots"
    return f"{total:,} {noun} unavailable on source records ({details})"


__all__ = ["COMPLETE_COVERAGE_STATUS", "coverage_status_label"]
