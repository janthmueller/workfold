"""Canonical public identifiers for selectable timestamp evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import get_close_matches
from enum import Enum
from functools import cache

from wuf.domain.observations import ActivityMarker, EntryType, RecordKind, Source, TimestampKind


class EvidenceKind(str, Enum):
    """One exact record/timestamp combination selectable by a user."""

    GIT_COMMIT_AUTHOR = "git:commit:author"
    GIT_COMMIT_COMMITTER = "git:commit:committer"
    GIT_FILE_CHANGE_AUTHOR = "git:file-change:author"
    GIT_FILE_CHANGE_COMMITTER = "git:file-change:committer"
    GIT_TAG_TAGGER = "git:tag:tagger"
    GIT_REFLOG_UPDATE = "git:reflog:update"
    FS_FILE_BIRTH = "fs:file:birth"
    FS_FILE_MODIFIED = "fs:file:modified"
    FS_FILE_METADATA_CHANGED = "fs:file:metadata-changed"
    FS_FILE_ACCESSED = "fs:file:accessed"
    FS_DIRECTORY_BIRTH = "fs:directory:birth"
    FS_DIRECTORY_MODIFIED = "fs:directory:modified"
    FS_DIRECTORY_METADATA_CHANGED = "fs:directory:metadata-changed"
    FS_DIRECTORY_ACCESSED = "fs:directory:accessed"
    FS_SYMLINK_BIRTH = "fs:symlink:birth"
    FS_SYMLINK_MODIFIED = "fs:symlink:modified"
    FS_SYMLINK_METADATA_CHANGED = "fs:symlink:metadata-changed"
    FS_SYMLINK_ACCESSED = "fs:symlink:accessed"

    @property
    def source(self) -> Source:
        """Return the collector source that owns this evidence kind."""

        return _EVIDENCE_DIMENSIONS[self][0]

    @property
    def record_kind(self) -> RecordKind:
        """Return the source-record family that owns this evidence kind."""

        return _EVIDENCE_DIMENSIONS[self][1]

    @property
    def timestamp_kind(self) -> TimestampKind:
        """Return the exact timestamp role selected by this evidence kind."""

        return _EVIDENCE_DIMENSIONS[self][2]

    @property
    def entry_type(self) -> EntryType | None:
        """Return the filesystem entry type, or ``None`` for Git evidence."""

        return _EVIDENCE_DIMENSIONS[self][3]

    @classmethod
    def from_dimensions(
        cls,
        record_kind: RecordKind,
        timestamp_kind: TimestampKind,
        entry_type: EntryType | None = None,
    ) -> EvidenceKind:
        """Resolve normalized record/timestamp dimensions to a public identifier."""

        try:
            return _DIMENSION_EVIDENCE[(record_kind, timestamp_kind, entry_type)]
        except KeyError as error:
            dimensions = f"{record_kind.value}/{timestamp_kind.value}"
            if entry_type is not None:
                dimensions += f"/{entry_type.value}"
            raise ValueError(f"unsupported evidence dimensions {dimensions}") from error


_EVIDENCE_DIMENSIONS: dict[EvidenceKind, tuple[Source, RecordKind, TimestampKind, EntryType | None]] = {
    EvidenceKind.GIT_COMMIT_AUTHOR: (Source.GIT, RecordKind.COMMIT, TimestampKind.GIT_AUTHOR, None),
    EvidenceKind.GIT_COMMIT_COMMITTER: (Source.GIT, RecordKind.COMMIT, TimestampKind.GIT_COMMITTER, None),
    EvidenceKind.GIT_FILE_CHANGE_AUTHOR: (
        Source.GIT,
        RecordKind.GIT_FILE_CHANGE,
        TimestampKind.GIT_AUTHOR,
        None,
    ),
    EvidenceKind.GIT_FILE_CHANGE_COMMITTER: (
        Source.GIT,
        RecordKind.GIT_FILE_CHANGE,
        TimestampKind.GIT_COMMITTER,
        None,
    ),
    EvidenceKind.GIT_TAG_TAGGER: (Source.GIT, RecordKind.TAG, TimestampKind.GIT_TAGGER, None),
    EvidenceKind.GIT_REFLOG_UPDATE: (Source.GIT, RecordKind.REFLOG, TimestampKind.GIT_REFLOG, None),
    **{
        evidence: (Source.FILESYSTEM, RecordKind.FILESYSTEM_ENTRY, timestamp, entry_type)
        for entry_type, values in (
            (
                EntryType.REGULAR_FILE,
                (
                    (EvidenceKind.FS_FILE_BIRTH, TimestampKind.FS_CREATED),
                    (EvidenceKind.FS_FILE_MODIFIED, TimestampKind.FS_MODIFIED),
                    (EvidenceKind.FS_FILE_METADATA_CHANGED, TimestampKind.FS_METADATA_CHANGED),
                    (EvidenceKind.FS_FILE_ACCESSED, TimestampKind.FS_ACCESSED),
                ),
            ),
            (
                EntryType.DIRECTORY,
                (
                    (EvidenceKind.FS_DIRECTORY_BIRTH, TimestampKind.FS_CREATED),
                    (EvidenceKind.FS_DIRECTORY_MODIFIED, TimestampKind.FS_MODIFIED),
                    (EvidenceKind.FS_DIRECTORY_METADATA_CHANGED, TimestampKind.FS_METADATA_CHANGED),
                    (EvidenceKind.FS_DIRECTORY_ACCESSED, TimestampKind.FS_ACCESSED),
                ),
            ),
            (
                EntryType.SYMLINK,
                (
                    (EvidenceKind.FS_SYMLINK_BIRTH, TimestampKind.FS_CREATED),
                    (EvidenceKind.FS_SYMLINK_MODIFIED, TimestampKind.FS_MODIFIED),
                    (EvidenceKind.FS_SYMLINK_METADATA_CHANGED, TimestampKind.FS_METADATA_CHANGED),
                    (EvidenceKind.FS_SYMLINK_ACCESSED, TimestampKind.FS_ACCESSED),
                ),
            ),
        )
        for evidence, timestamp in values
    },
}

_DIMENSION_EVIDENCE = {
    (record_kind, timestamp_kind, entry_type): evidence
    for evidence, (_source, record_kind, timestamp_kind, entry_type) in _EVIDENCE_DIMENSIONS.items()
}

_EVIDENCE_BITS = {kind: 1 << index for index, kind in enumerate(EvidenceKind)}
_ALL_EVIDENCE_BITS = sum(_EVIDENCE_BITS.values())

_SELECTOR_PATTERN = re.compile(r"[a-z0-9-]+(?::(?:[a-z0-9-]+|\*))*|\*")


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    """A non-empty, canonically ordered set of exact evidence kinds."""

    kinds: tuple[EvidenceKind, ...]

    def __post_init__(self) -> None:
        _require_evidence_kinds(tuple(self.kinds))
        canonical = tuple(kind for kind in EvidenceKind if kind in self.kinds)
        if not self.kinds:
            raise ValueError("an evidence selection cannot be empty")
        if canonical != self.kinds:
            raise ValueError("evidence kinds must be unique and canonically ordered")

    @classmethod
    def create(cls, kinds: Iterable[EvidenceKind]) -> EvidenceSelection:
        """Create a canonical selection from any iterable of evidence kinds."""

        values = tuple(kinds)
        _require_evidence_kinds(values)
        selected = frozenset(values)
        return cls(tuple(kind for kind in EvidenceKind if kind in selected))

    @property
    def sources(self) -> tuple[Source, ...]:
        """Return enabled sources in stable domain order."""

        selected = {kind.source for kind in self.kinds}
        return tuple(source for source in Source if source in selected)

    def includes_source(self, source: Source) -> bool:
        return any(kind.source is source for kind in self.kinds)

    def kinds_for_record(self, record_kind: RecordKind) -> tuple[EvidenceKind, ...]:
        return tuple(kind for kind in self.kinds if kind.record_kind is record_kind)

    def timestamp_kinds_for(self, record_kind: RecordKind) -> tuple[TimestampKind, ...]:
        selected = {kind.timestamp_kind for kind in self.kinds_for_record(record_kind)}
        return tuple(kind for kind in TimestampKind if kind in selected)

    def timestamp_kinds_for_entry(self, entry_type: EntryType) -> tuple[TimestampKind, ...]:
        """Return requested filesystem timestamp roles for one entry type."""

        return tuple(
            kind.timestamp_kind
            for kind in self.kinds
            if kind.record_kind is RecordKind.FILESYSTEM_ENTRY and kind.entry_type is entry_type
        )


def expand_evidence_selectors(selectors: tuple[str, ...], *, option: str) -> EvidenceSelection:
    """Expand validated exact/wildcard selectors against the supported catalog."""

    if not selectors:
        raise ValueError(f"{option} requires one or more event selectors")

    selected: set[EvidenceKind] = set()
    for raw in selectors:
        selector = raw.strip().lower()
        if not selector:
            raise ValueError(f"{option} selectors cannot be empty")
        if not _SELECTOR_PATTERN.fullmatch(selector):
            raise ValueError(
                f"invalid {option} selector {raw!r}; use colon-separated event identifiers and '*' wildcards"
            )
        regex = re.compile("^" + re.escape(selector).replace(r"\*", ".*") + "$")
        matches = tuple(kind for kind in EvidenceKind if regex.fullmatch(kind.value))
        if not matches:
            suggestion = _selector_suggestion(selector)
            suffix = f"; did you mean {suggestion!r}?" if suggestion is not None else ""
            raise ValueError(f"unknown {option} selector {raw!r}{suffix}")
        selected.update(matches)
    return EvidenceSelection.create(selected)


def evidence_mask(kinds: Iterable[EvidenceKind]) -> int:
    """Return the compact, lossless bit mask for one or more evidence kinds."""

    values = tuple(kinds)
    _require_evidence_kinds(values)
    if not values:
        raise ValueError("an evidence mask cannot be empty")
    return _cached_evidence_mask(values)


@cache
def _cached_evidence_mask(kinds: tuple[EvidenceKind, ...]) -> int:
    mask = 0
    for kind in kinds:
        mask |= _EVIDENCE_BITS[kind]
    return mask


def evidence_kinds_from_mask(mask: object) -> tuple[EvidenceKind, ...]:
    """Decode a validated evidence bit mask in canonical catalog order."""

    if not isinstance(mask, int) or isinstance(mask, bool):
        raise TypeError("an evidence mask must be an integer")
    if mask <= 0 or mask & ~_ALL_EVIDENCE_BITS:
        raise ValueError("an evidence mask must contain only supported event kinds")
    return _cached_evidence_kinds(mask)


@cache
def _cached_evidence_kinds(mask: int) -> tuple[EvidenceKind, ...]:
    return tuple(kind for kind in EvidenceKind if mask & _EVIDENCE_BITS[kind])


def marker_evidence_mask(marker: ActivityMarker) -> int:
    """Project every retained timestamp role of one plotted marker."""

    return evidence_mask(
        EvidenceKind.from_dimensions(
            observation.origin.record_kind,
            observation.kind,
            observation.origin.entry_type,
        )
        for observation in marker.observations
    )


def evidence_mask_source(mask: int) -> Source:
    """Return the single collector source represented by a marker mask."""

    evidence_kinds_from_mask(mask)
    return _cached_evidence_mask_source(mask)


@cache
def _cached_evidence_mask_source(mask: int) -> Source:
    sources = {kind.source for kind in _cached_evidence_kinds(mask)}
    if len(sources) != 1:
        raise ValueError("a marker evidence mask must belong to one source")
    return next(iter(sources))


def supported_marker_evidence_masks() -> tuple[int, ...]:
    """Return every evidence signature the current coalescing model can plot."""

    singletons = tuple(evidence_mask((kind,)) for kind in EvidenceKind)
    coalesced = (
        evidence_mask((EvidenceKind.GIT_COMMIT_AUTHOR, EvidenceKind.GIT_COMMIT_COMMITTER)),
        evidence_mask((EvidenceKind.GIT_FILE_CHANGE_AUTHOR, EvidenceKind.GIT_FILE_CHANGE_COMMITTER)),
    )
    return (*singletons, *coalesced)


def _selector_suggestion(selector: str) -> str | None:
    if selector.startswith("git:committer"):
        return "git:*:committer"
    if selector.startswith("git:author"):
        return "git:*:author"
    if selector in {"fs:created", "fs:birth"}:
        return "fs:file:birth"
    candidates = [kind.value for kind in EvidenceKind]
    normalized = selector.replace("*", "")
    matches = get_close_matches(normalized, candidates, n=1, cutoff=0.45)
    if matches:
        return matches[0]
    return None


def _require_evidence_kinds(values: tuple[object, ...]) -> None:
    if any(not isinstance(kind, EvidenceKind) for kind in values):
        raise TypeError("evidence selections accept only EvidenceKind values")


__all__ = [
    "EvidenceKind",
    "EvidenceSelection",
    "evidence_kinds_from_mask",
    "evidence_mask",
    "evidence_mask_source",
    "expand_evidence_selectors",
    "marker_evidence_mask",
    "supported_marker_evidence_masks",
]
