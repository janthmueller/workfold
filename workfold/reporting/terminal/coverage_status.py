"""Compact coverage status for normal terminal output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from workfold.application.collection import Collection
from workfold.configuration.options import RunOptions
from workfold.domain.coverage import CapabilityStatus, CoverageLedger
from workfold.domain.observations import TimestampKind
from workfold.reporting.terminal.coverage_scope import pruned_ignored_subtree_label, timestamp_label

COMPLETE_COVERAGE_STATUS = "complete for all discoverable timestamps in the requested scope"


def coverage_status_label(
    collection: Collection,
    ledger: CoverageLedger,
    options: RunOptions,
) -> str:
    """Summarize whether enabled collectors completed their requested scope."""

    error_count, _warning_count, _info_count = collection.diagnostic_counts
    filesystem_inventory_incomplete = sum(
        item.completeness_failure_count
        for item in collection.diagnostics
        if item.code == "git_filesystem_inventory_incomplete"
    )
    other_partial_warning_count = sum(
        item.completeness_failure_count
        for item in collection.diagnostics
        if item.code != "git_filesystem_inventory_incomplete"
    )
    if error_count or filesystem_inventory_incomplete or other_partial_warning_count or ledger.has_operational_errors:
        reasons: list[str] = []
        if filesystem_inventory_incomplete:
            reasons.append(
                "filesystem inventory incomplete"
                if filesystem_inventory_incomplete == 1
                else f"{filesystem_inventory_incomplete:,} filesystem inventories incomplete"
            )
        if error_count:
            reasons.append(_counted(error_count, "collection error"))
        if other_partial_warning_count:
            reasons.append(_counted(other_partial_warning_count, "collection warning"))
        if not reasons:
            reasons.append("collection incomplete")
        label = "partial · " + " · ".join(reasons)
    else:
        label = COMPLETE_COVERAGE_STATUS
    qualifiers: list[str] = []
    if options.git_identities:
        qualifiers.append("Git identity scope active")
    if options.exclusions:
        qualifiers.append("explicit exclusions active")
    pruned_ignored_subtrees = (
        collection.filesystem_result.accounting.pruned_ignored_subtrees
        if collection.filesystem_result is not None
        else 0
    )
    if pruned_ignored_subtrees:
        qualifiers.append(pruned_ignored_subtree_label(pruned_ignored_subtrees))
    unsupported_capabilities: dict[str, str | None] = {}
    for capability in collection.capabilities:
        if capability.status is CapabilityStatus.UNSUPPORTED:
            unsupported_capabilities.setdefault(capability.name, capability.note)
    for name, note in unsupported_capabilities.items():
        qualifier = f"{name} unavailable"
        if note:
            qualifier += f": {note}"
        qualifiers.append(qualifier)
    unavailable_by_kind = Counter[TimestampKind]()
    for item in ledger.timestamps:
        unavailable_by_kind[item.key.timestamp_kind] += item.unavailable
    unavailable_by_kind += Counter()
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
