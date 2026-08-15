"""Translate canonical evidence selections into exact collector activation."""

from __future__ import annotations

from dataclasses import dataclass

from workfold.domain.evidence import EvidenceSelection
from workfold.domain.observations import EntryType, RecordKind, Source, TimestampKind


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    """Exact record/timestamp partitions required for one collection pass."""

    commit_timestamps: tuple[TimestampKind, ...]
    file_change_timestamps: tuple[TimestampKind, ...]
    collect_tags: bool
    collect_reflogs: bool
    filesystem_timestamps: tuple[tuple[EntryType, tuple[TimestampKind, ...]], ...]

    @classmethod
    def from_selection(cls, selection: EvidenceSelection) -> CollectionPlan:
        """Build a collector plan without retaining parallel scope state."""

        return cls(
            commit_timestamps=selection.timestamp_kinds_for(RecordKind.COMMIT),
            file_change_timestamps=selection.timestamp_kinds_for(RecordKind.GIT_FILE_CHANGE),
            collect_tags=bool(selection.kinds_for_record(RecordKind.TAG)),
            collect_reflogs=bool(selection.kinds_for_record(RecordKind.REFLOG)),
            filesystem_timestamps=tuple(
                (entry_type, kinds)
                for entry_type in EntryType
                if (kinds := selection.timestamp_kinds_for_entry(entry_type))
            ),
        )

    @property
    def includes_git(self) -> bool:
        return bool(self.commit_timestamps or self.file_change_timestamps or self.collect_tags or self.collect_reflogs)

    @property
    def includes_filesystem(self) -> bool:
        return bool(self.filesystem_timestamps)

    @property
    def commit_scan_timestamps(self) -> tuple[TimestampKind, ...]:
        """Return roles needed to discover commits or derive file changes."""

        requested = {*self.commit_timestamps, *self.file_change_timestamps}
        return tuple(kind for kind in (TimestampKind.GIT_AUTHOR, TimestampKind.GIT_COMMITTER) if kind in requested)

    def includes_source(self, source: Source) -> bool:
        return self.includes_git if source is Source.GIT else self.includes_filesystem


__all__ = ["CollectionPlan"]
