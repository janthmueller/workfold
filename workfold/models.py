"""Immutable, renderer-neutral domain models used throughout Workfold."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path

from workfold.provenance import activity_marker_id, observation_id


class Source(str, Enum):
    """A semantic source of activity evidence."""

    GIT = "git"
    FILESYSTEM = "filesystem"


class RecordKind(str, Enum):
    """A discoverable source-record kind."""

    COMMIT = "commit"
    GIT_FILE_CHANGE = "git_file_change"
    TAG = "tag"
    REFLOG = "reflog"
    FILESYSTEM_ENTRY = "filesystem_entry"


class TimestampKind(str, Enum):
    """The exact semantic role of a captured timestamp."""

    GIT_AUTHOR = "git_author"
    GIT_COMMITTER = "git_committer"
    GIT_TAGGER = "git_tagger"
    GIT_REFLOG = "git_reflog"
    FS_CREATED = "fs_created"
    FS_MODIFIED = "fs_modified"
    FS_METADATA_CHANGED = "fs_metadata_changed"
    FS_ACCESSED = "fs_accessed"

    @property
    def source(self) -> Source:
        """Return the only source capable of producing this timestamp kind."""

        if self.value.startswith("git_"):
            return Source.GIT
        return Source.FILESYSTEM


class EntryType(str, Enum):
    """Filesystem entry types supported by the MVP."""

    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class GitChangeKind(str, Enum):
    """Normalized Git file-change semantics."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    OTHER = "other"


class Weekday(IntEnum):
    """A weekday using :meth:`datetime.datetime.weekday` numbering."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6

    @property
    def abbreviation(self) -> str:
        """Return the canonical two-letter schedule token."""

        return ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")[int(self)]

    @property
    def is_weekend(self) -> bool:
        """Return whether this is Saturday or Sunday."""

        return self >= Weekday.SATURDAY


@dataclass(frozen=True, slots=True)
class RecordOrigin:
    """One semantic source record with exact provenance metadata."""

    record_id: str
    source: Source
    record_kind: RecordKind
    repository_or_root: Path
    path: Path | None = None
    old_path: Path | None = None
    commit_id: str | None = None
    object_id: str | None = None
    target_id: str | None = None
    ref_name: str | None = None
    diff_basis: str | None = None
    change_kind: GitChangeKind | None = None
    entry_type: EntryType | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("record_id must not be empty")
        if self.record_kind is RecordKind.FILESYSTEM_ENTRY and self.source is not Source.FILESYSTEM:
            raise ValueError("filesystem entry origins require the filesystem source")
        if self.record_kind is not RecordKind.FILESYSTEM_ENTRY and self.source is not Source.GIT:
            raise ValueError("Git record origins require the Git source")
        if self.record_kind is RecordKind.COMMIT and self.commit_id is None:
            raise ValueError("commit origins require a commit_id")
        if self.record_kind is RecordKind.GIT_FILE_CHANGE:
            if self.commit_id is None or self.path is None or self.diff_basis is None or self.change_kind is None:
                raise ValueError("Git file-change origins require commit, path, diff-basis, and change provenance")
        if self.record_kind is RecordKind.TAG and (self.ref_name is None or self.target_id is None):
            raise ValueError("tag origins require ref and target identities")
        if self.record_kind is RecordKind.REFLOG and (
            self.ref_name is None or self.object_id is None or self.target_id is None
        ):
            raise ValueError("reflog origins require ref, old-object, and new-object identities")
        if self.record_kind is RecordKind.FILESYSTEM_ENTRY and self.path is None:
            raise ValueError("filesystem entry origins require a path")
        if self.change_kind is not None and self.record_kind is not RecordKind.GIT_FILE_CHANGE:
            raise ValueError("change_kind is valid only for Git file-change records")
        if (
            self.old_path is not None or self.diff_basis is not None
        ) and self.record_kind is not RecordKind.GIT_FILE_CHANGE:
            raise ValueError("old_path and diff_basis are valid only for Git file-change records")
        if self.entry_type is not None and self.record_kind is not RecordKind.FILESYSTEM_ENTRY:
            raise ValueError("entry_type is valid only for filesystem-entry records")
        if self.path is not None and self.record_kind not in {
            RecordKind.GIT_FILE_CHANGE,
            RecordKind.FILESYSTEM_ENTRY,
        }:
            raise ValueError("path is valid only for file-change and filesystem-entry records")
        if self.commit_id is not None and self.record_kind not in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE}:
            raise ValueError("commit_id is valid only for commit and file-change records")
        if (
            self.object_id is not None or self.target_id is not None or self.ref_name is not None
        ) and self.record_kind not in {
            RecordKind.TAG,
            RecordKind.REFLOG,
        }:
            raise ValueError("object, target, and ref identities are valid only for tag and reflog records")

    @property
    def provenance_id(self) -> str:
        """Compatibility name emphasizing that ``record_id`` is provenance."""

        return self.record_id


@dataclass(frozen=True, slots=True)
class TimestampObservation:
    """A successfully extracted timezone-aware instant and its provenance."""

    observation_id: str
    origin: RecordOrigin
    kind: TimestampKind
    instant_utc_ns: int
    raw_timestamp: str
    original_offset_minutes: int | None = None
    actor_name: str | None = None
    actor_email: str | None = None

    def __post_init__(self) -> None:
        if not self.observation_id:
            raise ValueError("observation_id must not be empty")
        if not self.raw_timestamp:
            raise ValueError("raw_timestamp must not be empty")
        if self.kind.source is not self.origin.source:
            raise ValueError("timestamp kind does not belong to the origin source")
        if self.original_offset_minutes is not None and not -1439 <= self.original_offset_minutes <= 1439:
            raise ValueError("original offset must be between -1439 and 1439 minutes")
        if self.origin.source is Source.GIT:
            if self.original_offset_minutes is None:
                raise ValueError("Git observations require their recorded UTC offset")
            if self.actor_name is None or self.actor_email is None:
                raise ValueError("Git observations require their recorded identity")
        elif any(value is not None for value in (self.original_offset_minutes, self.actor_name, self.actor_email)):
            raise ValueError("filesystem observations cannot carry Git identity or offset metadata")

    @classmethod
    def create(
        cls,
        origin: RecordOrigin,
        kind: TimestampKind,
        instant_utc_ns: int,
        raw_timestamp: str,
        *,
        original_offset_minutes: int | None = None,
        actor_name: str | None = None,
        actor_email: str | None = None,
    ) -> TimestampObservation:
        """Create an observation with its deterministic identity."""

        return cls(
            observation_id(origin.record_id, kind.value),
            origin,
            kind,
            instant_utc_ns,
            raw_timestamp,
            original_offset_minutes,
            actor_name,
            actor_email,
        )

    @property
    def timestamp_kind(self) -> TimestampKind:
        """Return the timestamp kind using the conceptual-model name."""

        return self.kind

    @property
    def epoch_ns(self) -> int:
        """Return the UTC epoch nanoseconds using a concise adapter name."""

        return self.instant_utc_ns


@dataclass(frozen=True, slots=True)
class ActivityMarker:
    """One chart event, possibly coalescing one author/committer pair."""

    marker_id: str
    occurred_at_utc_ns: int
    observations: tuple[TimestampObservation, ...]

    def __post_init__(self) -> None:
        if not self.marker_id:
            raise ValueError("marker_id must not be empty")
        if not self.observations:
            raise ValueError("an activity marker needs at least one observation")
        if len(self.observations) > 2:
            raise ValueError("an activity marker may contain at most two observations")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("an activity marker cannot contain duplicate observations")
        if any(item.instant_utc_ns != self.occurred_at_utc_ns for item in self.observations):
            raise ValueError("all marker observations must have the marker instant")
        first_observation = next(iter(self.observations))
        if any(item.origin.record_id != first_observation.origin.record_id for item in self.observations):
            raise ValueError("coalesced observations must belong to the same record")
        if len(self.observations) == 2 and {item.kind for item in self.observations} != {
            TimestampKind.GIT_AUTHOR,
            TimestampKind.GIT_COMMITTER,
        }:
            raise ValueError("only a Git author/committer pair may be coalesced")

    @classmethod
    def create(cls, observations: Iterable[TimestampObservation]) -> ActivityMarker:
        """Create and validate a marker with deterministic observation ordering."""

        ordered = tuple(sorted(observations, key=lambda item: (item.kind.value, item.observation_id)))
        if not ordered:
            raise ValueError("an activity marker needs at least one observation")
        return cls(
            activity_marker_id(tuple(item.observation_id for item in ordered)),
            ordered[0].instant_utc_ns,
            ordered,
        )

    @property
    def origin(self) -> RecordOrigin:
        """Return the shared origin of all constituent observations."""

        return self.observations[0].origin

    @property
    def timestamp_roles(self) -> tuple[TimestampKind, ...]:
        """Return every atomic timestamp role retained by this marker."""

        return tuple(item.kind for item in self.observations)

    @property
    def sources(self) -> frozenset[Source]:
        """Return the marker's semantic sources (one in the MVP model)."""

        return frozenset(item.origin.source for item in self.observations)


@dataclass(frozen=True, slots=True)
class ClassifiedMarker:
    """A marker localized and classified without terminal-layout concerns.

    ``local_datetime`` intentionally preserves the localized wall-clock value
    while the marker retains the authoritative nanosecond UTC instant.  Sparse
    chart layout derives its own clusters later; classification never rounds an
    observation into a renderer-specific bin.
    """

    marker: ActivityMarker
    local_datetime: datetime
    within_schedule: bool

    def __post_init__(self) -> None:
        if self.local_datetime.tzinfo is None or self.local_datetime.utcoffset() is None:
            raise ValueError("local_datetime must be timezone-aware")

    @property
    def weekday(self) -> Weekday:
        """Return the localized weekday used by the folded chart."""

        return Weekday(self.local_datetime.weekday())

    @property
    def minute_of_day(self) -> int:
        """Return the containing wall-clock minute for schedule/crop policy."""

        return self.local_datetime.hour * 60 + self.local_datetime.minute

    @property
    def time_of_day_ns(self) -> int:
        """Return the exact local wall-clock offset after midnight.

        ``datetime`` stores microseconds, but filesystem observations may carry
        a sub-microsecond remainder.  UTC offsets have whole-second precision,
        so the original instant's nanosecond remainder is also the localized
        wall-clock remainder and can be restored without inventing precision.
        """

        seconds = self.minute_of_day * 60 + self.local_datetime.second
        return (
            seconds * 1_000_000_000 + self.local_datetime.microsecond * 1_000 + self.marker.occurred_at_utc_ns % 1_000
        )

    @property
    def weekend(self) -> bool:
        """Return whether the localized marker falls on Saturday or Sunday."""

        return self.weekday.is_weekend


def coalesce_observations(observations: Iterable[TimestampObservation]) -> tuple[ActivityMarker, ...]:
    """Coalesce only same-record/same-instant Git author/committer pairs."""

    grouped: dict[tuple[str, int], list[TimestampObservation]] = {}
    for item in observations:
        grouped.setdefault((item.origin.record_id, item.instant_utc_ns), []).append(item)

    markers: list[ActivityMarker] = []
    for group in grouped.values():
        authors = [item for item in group if item.kind is TimestampKind.GIT_AUTHOR]
        committers = [item for item in group if item.kind is TimestampKind.GIT_COMMITTER]
        paired_ids: set[str] = set()
        if len(authors) == 1 and len(committers) == 1:
            markers.append(ActivityMarker.create((authors[0], committers[0])))
            paired_ids.update((authors[0].observation_id, committers[0].observation_id))
        markers.extend(ActivityMarker.create((item,)) for item in group if item.observation_id not in paired_ids)

    return tuple(sorted(markers, key=lambda marker: (marker.occurred_at_utc_ns, marker.marker_id)))


__all__ = [
    "ActivityMarker",
    "ClassifiedMarker",
    "EntryType",
    "GitChangeKind",
    "RecordKind",
    "RecordOrigin",
    "Source",
    "TimestampKind",
    "TimestampObservation",
    "Weekday",
    "coalesce_observations",
]
