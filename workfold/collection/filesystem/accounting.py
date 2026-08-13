"""Mutable coverage accounting used during filesystem collection."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from workfold.collection.filesystem.models import FilesystemAccounting, TimestampExtractionCoverage
from workfold.domain.coverage import (
    ExtractionDisposition,
    RecordCoverage,
    RecordCoverageKey,
    RecordDisposition,
    TimestampCoverageKey,
)
from workfold.domain.observations import RecordKind, Source, TimestampKind


@dataclass(slots=True)
class AccountingBuilder:
    """Accumulate reconciled record and timestamp outcomes by scan root."""

    retain_scope_match_ids: bool = True
    _discovered: dict[RecordCoverageKey, int] = field(default_factory=lambda: {})
    _records: dict[tuple[RecordCoverageKey, RecordDisposition], int] = field(default_factory=lambda: {})
    _requested: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    _extractions: dict[tuple[TimestampCoverageKey, ExtractionDisposition], int] = field(default_factory=lambda: {})
    _scope_matches: dict[TimestampCoverageKey, int] = field(default_factory=lambda: {})
    # Retained IDs belong to scope matches, not every extracted value. They
    # let the retaining adapter prove that every match reached the pipeline.
    _scope_match_ids: dict[TimestampCoverageKey, list[str]] = field(default_factory=lambda: {})
    _record_keys: dict[Path, RecordCoverageKey] = field(default_factory=lambda: {})
    _timestamp_keys: dict[tuple[Path, TimestampKind], TimestampCoverageKey] = field(default_factory=lambda: {})
    _pruned_ignored_subtrees: int = 0

    def _record_key(self, root: Path) -> RecordCoverageKey:
        key = self._record_keys.get(root)
        if key is None:
            key = record_key(root)
            self._record_keys[root] = key
        return key

    def _timestamp_key(self, root: Path, kind: TimestampKind) -> TimestampCoverageKey:
        partition = (root, kind)
        key = self._timestamp_keys.get(partition)
        if key is None:
            key = timestamp_key(root, kind)
            self._timestamp_keys[partition] = key
        return key

    def ensure_root(self, root: Path, kinds: Sequence[TimestampKind]) -> None:
        record = self._record_key(root)
        self._discovered.setdefault(record, 0)
        for kind in kinds:
            key = self._timestamp_key(root, kind)
            self._requested.setdefault(key, 0)
            self._scope_match_ids.setdefault(key, [])

    def discover(self, root: Path, count: int = 1) -> None:
        if count < 0:
            raise ValueError("filesystem discovery count must be non-negative")
        key = self._record_key(root)
        self._discovered[key] = self._discovered.get(key, 0) + count

    def record(self, root: Path, disposition: RecordDisposition, count: int = 1) -> None:
        if count < 0:
            raise ValueError("filesystem record count must be non-negative")
        key = (self._record_key(root), disposition)
        self._records[key] = self._records.get(key, 0) + count

    def prune_ignored_subtree(self) -> None:
        """Account for one ignored directory whose descendants were not walked."""

        self._pruned_ignored_subtrees += 1

    def request(self, root: Path, kind: TimestampKind) -> None:
        key = self._timestamp_key(root, kind)
        self._requested[key] = self._requested.get(key, 0) + 1

    def extraction(
        self,
        root: Path,
        kind: TimestampKind,
        disposition: ExtractionDisposition,
    ) -> None:
        key = self._timestamp_key(root, kind)
        outcome = (key, disposition)
        self._extractions[outcome] = self._extractions.get(outcome, 0) + 1

    def match_scope(self, root: Path, kind: TimestampKind, observation_id: str | None = None) -> None:
        """Record one extracted filesystem timestamp matching query scope."""

        key = self._timestamp_key(root, kind)
        self._scope_matches[key] = self._scope_matches.get(key, 0) + 1
        if observation_id is not None and self.retain_scope_match_ids:
            self._scope_match_ids.setdefault(key, []).append(observation_id)

    def build(self) -> FilesystemAccounting:
        record_keys = set(self._discovered)
        record_keys.update(key for key, _ in self._records)
        timestamp_keys = set(self._requested)
        timestamp_keys.update(key for key, _ in self._extractions)
        timestamp_keys.update(self._scope_matches)
        timestamp_keys.update(self._scope_match_ids)
        records = tuple(
            RecordCoverage(
                key=key,
                discovered=self._discovered.get(key, 0),
                eligible=self._records.get((key, RecordDisposition.ELIGIBLE), 0),
                ignored=self._records.get((key, RecordDisposition.IGNORED), 0),
                explicitly_excluded=self._records.get((key, RecordDisposition.EXPLICITLY_EXCLUDED), 0),
                excluded_entry_type=self._records.get((key, RecordDisposition.EXCLUDED_ENTRY_TYPE), 0),
                semantic_git_admin=self._records.get((key, RecordDisposition.SEMANTIC_GIT_ADMIN), 0),
                record_errors=self._records.get((key, RecordDisposition.RECORD_ERROR), 0),
            )
            for key in sorted(record_keys, key=lambda item: item.target)
        )
        timestamps = tuple(
            TimestampExtractionCoverage(
                key=key,
                requested=self._requested.get(key, 0),
                captured=self._extractions.get((key, ExtractionDisposition.CAPTURED), 0),
                unavailable=self._extractions.get((key, ExtractionDisposition.UNAVAILABLE), 0),
                unsupported=self._extractions.get((key, ExtractionDisposition.UNSUPPORTED), 0),
                errors=self._extractions.get((key, ExtractionDisposition.ERROR), 0),
                scope_matches=self._scope_matches.get(key, 0),
                scope_match_ids=tuple(self._scope_match_ids.get(key, ())),
                scope_match_ids_complete=self.retain_scope_match_ids,
            )
            for key in sorted(timestamp_keys, key=lambda item: (item.target, item.timestamp_kind.value))
        )
        return FilesystemAccounting(records, timestamps, self._pruned_ignored_subtrees)


def record_key(root: Path) -> RecordCoverageKey:
    return RecordCoverageKey(Source.FILESYSTEM, os.fspath(root), RecordKind.FILESYSTEM_ENTRY)


def timestamp_key(root: Path, kind: TimestampKind) -> TimestampCoverageKey:
    return TimestampCoverageKey(Source.FILESYSTEM, os.fspath(root), RecordKind.FILESYSTEM_ENTRY, kind)
