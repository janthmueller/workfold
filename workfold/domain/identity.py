"""Compact, exact Git identity projections for optional chart labels."""

from __future__ import annotations

from dataclasses import dataclass

from workfold.domain.observations import ActivityMarker, Source


@dataclass(frozen=True, slots=True)
class RecordedIdentity:
    """One unverified name/email pair exactly as recorded by Git."""

    name: str
    email: str


@dataclass(frozen=True, slots=True)
class MarkerIdentity:
    """The distinct recorded identities represented by one chart marker."""

    members: tuple[RecordedIdentity, ...]

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("a marker identity needs at least one recorded identity")
        if len(set(self.members)) != len(self.members):
            raise ValueError("a marker identity cannot contain duplicate identities")
        if self.members != tuple(sorted(self.members, key=recorded_identity_sort_key)):
            raise ValueError("recorded identities must use deterministic order")


def marker_identity(marker: ActivityMarker) -> MarkerIdentity | None:
    """Project one Git marker into its exact distinct recorded identities."""

    if marker.origin.source is not Source.GIT:
        return None
    identities = {
        RecordedIdentity(observation.actor_name or "", observation.actor_email or "")
        for observation in marker.observations
    }
    if not identities:
        raise ValueError("a Git marker must retain at least one recorded identity")
    return MarkerIdentity(tuple(sorted(identities, key=recorded_identity_sort_key)))


def recorded_identity_sort_key(identity: RecordedIdentity) -> tuple[str, str, str, str]:
    """Return a deterministic, case-aware ordering key without merging values."""

    return (identity.name.casefold(), identity.email.casefold(), identity.name, identity.email)


def marker_identity_sort_key(identity: MarkerIdentity) -> tuple[tuple[str, str, str, str], ...]:
    """Return a deterministic ordering key for identity registries and legends."""

    return tuple(recorded_identity_sort_key(member) for member in identity.members)


__all__ = [
    "MarkerIdentity",
    "RecordedIdentity",
    "marker_identity",
    "marker_identity_sort_key",
    "recorded_identity_sort_key",
]
