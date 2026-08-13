"""Normalized local Git tag records and reconciled collector results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workfold.collection.diagnostics import CollectorDiagnostic, diagnostics_are_partial
from workfold.collection.git.objects.models import GitSignature
from workfold.collection.git.repository import GitRepository
from workfold.domain.observations import RecordKind, RecordOrigin, Source, TimestampKind, TimestampObservation
from workfold.domain.provenance import git_tag_id


@dataclass(frozen=True, slots=True)
class DiscoveredGitTag:
    """One local tag ref as reported by ``for-each-ref``."""

    ref_name: str
    raw_ref_name: bytes
    object_id: str
    object_type: str

    @property
    def annotated(self) -> bool:
        """Whether the ref points at an independent tag object."""

        return self.object_type == "tag"


@dataclass(frozen=True, slots=True)
class ParsedTagObject:
    """Timestamp-relevant fields retained from one raw annotated tag object."""

    object_id: str
    target_id: str
    target_type: str
    tag_name: str
    raw_tag_name: bytes
    tagger: GitSignature | None
    subject: str
    raw_subject: bytes


@dataclass(frozen=True, slots=True)
class CollectedGitTag:
    """One local tag ref, including lightweight tag unavailability."""

    repository: GitRepository
    ref: DiscoveredGitTag
    target_id: str
    tagger: GitSignature | None
    subject: str | None = None

    @property
    def annotated(self) -> bool:
        return self.ref.annotated

    @property
    def tag_object_id(self) -> str | None:
        return self.ref.object_id if self.annotated else None

    def to_origin(self) -> RecordOrigin:
        """Create one record even when the tagger slot is unavailable."""

        return RecordOrigin(
            record_id=git_tag_id(
                self.repository.root,
                self.ref.ref_name,
                self.tag_object_id,
                self.target_id,
            ),
            source=Source.GIT,
            record_kind=RecordKind.TAG,
            repository_or_root=self.repository.root,
            object_id=self.tag_object_id,
            target_id=self.target_id,
            ref_name=self.ref.ref_name,
            description=self.subject,
        )

    def to_observation(self) -> TimestampObservation:
        """Return the independent tagger timestamp for an annotated tag."""

        if self.tagger is None:
            raise ValueError("this tag has no independent tagger timestamp")
        return TimestampObservation.create(
            self.to_origin(),
            TimestampKind.GIT_TAGGER,
            self.tagger.epoch_nanoseconds,
            self.tagger.raw_timestamp,
            original_offset_minutes=self.tagger.offset_seconds // 60,
            actor_name=self.tagger.identity.name,
            actor_email=self.tagger.identity.email,
        )


@dataclass(frozen=True, slots=True)
class GitTagRepositoryAccounting:
    """Reconciled tag and tagger-slot counters for one repository."""

    repository: GitRepository
    discovered_tags: int
    captured_tags: int
    record_errors: int
    annotated_tags: int
    lightweight_tags: int
    captured_tagger_timestamps: int
    unavailable_tagger_timestamps: int
    scope_matches: int
    unavailable_objects: int
    parse_errors: int
    operational_errors: int
    successful: bool

    def __post_init__(self) -> None:
        counters = (
            self.discovered_tags,
            self.captured_tags,
            self.record_errors,
            self.annotated_tags,
            self.lightweight_tags,
            self.captured_tagger_timestamps,
            self.unavailable_tagger_timestamps,
            self.scope_matches,
            self.unavailable_objects,
            self.parse_errors,
            self.operational_errors,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Git tag repository counters must be non-negative")
        if self.discovered_tags != self.captured_tags + self.record_errors:
            raise ValueError("Git tag repository record accounting does not reconcile")
        if self.discovered_tags != self.annotated_tags + self.lightweight_tags:
            raise ValueError("Git tag repository discovery accounting does not reconcile")
        if self.captured_tags != self.captured_tagger_timestamps + self.unavailable_tagger_timestamps:
            raise ValueError("Git tag repository timestamp accounting does not reconcile")
        if self.scope_matches > self.captured_tagger_timestamps:
            raise ValueError("Git tag scope matches exceed captured tagger timestamps")

    @property
    def repository_root(self) -> Path:
        """Filesystem root used as the repository coverage target."""

        return self.repository.root

    @property
    def repository_identity(self) -> str:
        """Canonical repository identity used for collection deduplication."""

        return self.repository.identity

    @property
    def eligible_tags(self) -> int:
        """Tag records eligible for a tagger timestamp slot."""

        return self.captured_tags


@dataclass(frozen=True, slots=True)
class GitTagCollectionResult:
    """Tag records plus explicit tagger availability accounting."""

    tags: tuple[CollectedGitTag, ...]
    diagnostics: tuple[CollectorDiagnostic, ...]
    requested_repositories: int
    successful_repositories: int
    discovered_tags: int
    annotated_tags: int
    lightweight_tags: int
    captured_tagger_timestamps: int
    unavailable_tagger_timestamps: int
    unavailable_objects: int
    parse_errors: int
    repository_accounting: tuple[GitTagRepositoryAccounting, ...] = ()
    records_retained: bool = True

    def __post_init__(self) -> None:
        captured_tags = sum(item.captured_tags for item in self.repository_accounting)
        if self.repository_accounting:
            if self.records_retained and len(self.tags) != captured_tags:
                raise ValueError("retained Git tags do not match repository accounting")
            if len(self.tags) > captured_tags:
                raise ValueError("retained Git tags exceed captured repository accounting")

    @property
    def is_partial(self) -> bool:
        return diagnostics_are_partial(self.diagnostics)
