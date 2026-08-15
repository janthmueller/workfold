"""Mutable construction of validated coverage ledgers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from workfold.domain.coverage.models import (
    CoverageLedger,
    ExtractionDisposition,
    PlottingDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverage,
    TimestampCoverageKey,
)


@dataclass(slots=True)
class CoverageLedgerBuilder:
    """Mutable collection-time builder that emits an immutable ledger."""

    _records_discovered: dict[RecordCoverageKey, int] = field(default_factory=lambda: {})
    _record_outcomes: dict[tuple[RecordCoverageKey, RecordDisposition], int] = field(default_factory=lambda: {})
    _slots_examined: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _extraction_outcomes: dict[tuple[TimestampCoverageKey, ExtractionDisposition], int] = field(
        default_factory=lambda: {}
    )
    _scope_matches: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _scope_errors: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _materialization_errors: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _selected_observations: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _plotting_outcomes: dict[tuple[TimestampCoverageKey, PlottingDisposition], int] = field(default_factory=lambda: {})

    def discover_record(self, key: RecordCoverageKey, count: int = 1) -> None:
        """Record how many source records were discovered."""

        _increment(self._records_discovered, key, count)

    def record_outcome(self, key: RecordCoverageKey, disposition: RecordDisposition, count: int = 1) -> None:
        """Assign discovered records to one terminal disposition."""

        _increment(self._record_outcomes, (key, disposition), count)

    def examine_slot(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record how many timestamp slots a collector examined."""

        _increment(self._slots_examined, key, count)

    def extraction_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: ExtractionDisposition,
        count: int = 1,
    ) -> None:
        """Assign examined slots to one terminal extraction disposition."""

        _increment(self._extraction_outcomes, (key, disposition), count)

    def select_observation(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record observations that matched the requested query scope."""

        _increment(self._selected_observations, key, count)

    def match_scope(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record readable timestamp values matching the requested scope."""

        _increment(self._scope_matches, key, count)

    def materialization_error(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record matching values that could not become observations."""

        _increment(self._materialization_errors, key, count)

    def scope_error(self, key: TimestampCoverageKey, count: int = 1) -> None:
        """Record readable values whose remaining scope could not be evaluated."""

        _increment(self._scope_errors, key, count)

    def plotting_outcome(
        self,
        key: TimestampCoverageKey,
        disposition: PlottingDisposition,
        count: int = 1,
    ) -> None:
        """Assign selected observations to a marker/coalescing disposition."""

        _increment(self._plotting_outcomes, (key, disposition), count)

    def add_ledger(self, ledger: CoverageLedger) -> None:
        """Add all counts from an immutable ledger snapshot."""

        for item in ledger.records:
            self.discover_record(item.key, item.discovered)
            for disposition in RecordDisposition:
                self.record_outcome(item.key, disposition, item.count(disposition))
        for item in ledger.timestamps:
            self.examine_slot(item.key, item.examined)
            for disposition in ExtractionDisposition:
                self.extraction_outcome(item.key, disposition, item.extraction_count(disposition))
            self.match_scope(item.key, item.scope_matches)
            self.scope_error(item.key, item.scope_errors)
            self.materialization_error(item.key, item.materialization_errors)
            self.select_observation(item.key, item.selected)
            for disposition in PlottingDisposition:
                self.plotting_outcome(item.key, disposition, item.plotting_count(disposition))

    def build(self, *, validate: bool = True) -> CoverageLedger:
        """Create a deterministic immutable snapshot, validating by default."""

        record_keys = set(self._records_discovered)
        record_keys.update(key for key, _ in self._record_outcomes)
        timestamp_keys = set(self._slots_examined)
        timestamp_keys.update(key for key, _ in self._extraction_outcomes)
        timestamp_keys.update(self._scope_matches)
        timestamp_keys.update(self._scope_errors)
        timestamp_keys.update(self._materialization_errors)
        timestamp_keys.update(self._selected_observations)
        timestamp_keys.update(key for key, _ in self._plotting_outcomes)

        records = tuple(
            RecordCoverage(
                key=key,
                discovered=self._records_discovered.get(key, 0),
                eligible=self._record_outcomes.get((key, RecordDisposition.ELIGIBLE), 0),
                ignored=self._record_outcomes.get((key, RecordDisposition.IGNORED), 0),
                explicitly_excluded=self._record_outcomes.get((key, RecordDisposition.EXPLICITLY_EXCLUDED), 0),
                excluded_entry_type=self._record_outcomes.get((key, RecordDisposition.EXCLUDED_ENTRY_TYPE), 0),
                semantic_git_admin=self._record_outcomes.get((key, RecordDisposition.SEMANTIC_GIT_ADMIN), 0),
                record_errors=self._record_outcomes.get((key, RecordDisposition.RECORD_ERROR), 0),
            )
            for key in sorted(record_keys, key=_record_key_sort)
        )
        timestamps = tuple(
            TimestampCoverage(
                key=key,
                examined=self._slots_examined.get(key, 0),
                values_read=self._extraction_outcomes.get((key, ExtractionDisposition.CAPTURED), 0),
                unavailable=self._extraction_outcomes.get((key, ExtractionDisposition.UNAVAILABLE), 0),
                unsupported=self._extraction_outcomes.get((key, ExtractionDisposition.UNSUPPORTED), 0),
                extraction_errors=self._extraction_outcomes.get((key, ExtractionDisposition.ERROR), 0),
                scope_matches=self._scope_matches.get(key, 0),
                materialization_errors=self._materialization_errors.get(key, 0),
                selected=self._selected_observations.get(key, 0),
                markers=self._plotting_outcomes.get((key, PlottingDisposition.MARKER), 0),
                coalesced_into_markers=self._plotting_outcomes.get((key, PlottingDisposition.COALESCED_INTO_MARKER), 0),
                scope_errors=self._scope_errors.get(key, 0),
            )
            for key in sorted(timestamp_keys, key=_timestamp_key_sort)
        )
        ledger = CoverageLedger(records, timestamps)
        if validate:
            ledger.validate()
        return ledger


_Key = TypeVar("_Key")


def _increment(counts: dict[_Key, int], key: _Key, count: int) -> None:
    if count < 0:
        raise ValueError("coverage increments must be non-negative")
    counts[key] = counts.get(key, 0) + count


def _record_key_sort(key: RecordCoverageKey) -> tuple[str, str, str]:
    return key.source.value, key.target, key.record_kind.value


def _timestamp_key_sort(key: TimestampCoverageKey) -> tuple[str, str, str, str, str]:
    return (
        key.source.value,
        key.target,
        key.record_kind.value,
        key.entry_type.value if key.entry_type else "",
        key.timestamp_kind.value,
    )


def merge_ledgers(*ledgers: CoverageLedger) -> CoverageLedger:
    """Add independent ledger partitions into one reconciled snapshot."""

    builder = CoverageLedgerBuilder()
    for ledger in ledgers:
        builder.add_ledger(ledger)
    return builder.build()


__all__ = ["CoverageLedgerBuilder", "merge_ledgers"]
