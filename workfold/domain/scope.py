"""Pure query-scope selection for normalized timestamp observations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from workfold.domain.observations import Source, TimestampObservation
from workfold.domain.time import InstantRangeUnion


class RefScope(str, Enum):
    """Reachable Git references included when discovering commits."""

    HEAD = "head"
    LOCAL_BRANCHES = "local-branches"
    ALL_REFS = "all-refs"


@dataclass(frozen=True, slots=True)
class ObservationScope:
    """Select timestamps by the requested date and Git identity scope.

    The scope is deliberately independent from collection, coverage, schedule
    classification, and rendering. Collectors may apply it to lightweight
    source metadata before constructing rich observations, provided they use
    the same predicate.
    """

    selected_range: InstantRangeUnion
    git_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "git_identities",
            tuple(value.casefold() for value in self.git_identities),
        )

    @property
    def selects_all_dates(self) -> bool:
        """Return whether the time scope contains every representable instant."""

        return self.selected_range.is_unbounded

    @property
    def filters_git_identities(self) -> bool:
        """Return whether Git identity participates in the requested scope."""

        return bool(self.git_identities)

    def is_restrictive_for(self, source: Source) -> bool:
        """Return whether this scope can exclude observations from ``source``."""

        return not self.selects_all_dates or (source is Source.GIT and self.filters_git_identities)

    def includes_timestamp(
        self,
        *,
        instant_utc_ns: int,
        source: Source,
        actor_name: str | None = None,
        actor_email: str | None = None,
    ) -> bool:
        """Return whether lightweight timestamp metadata belongs to the scope."""

        if not self.selected_range.contains(instant_utc_ns):
            return False
        if source is not Source.GIT or not self.git_identities:
            return True
        return _matches_git_identity(
            actor_name,
            actor_email,
            self.git_identities,
        )

    def includes(self, observation: TimestampObservation) -> bool:
        """Return whether one normalized observation belongs to the scope."""

        return self.includes_timestamp(
            instant_utc_ns=observation.instant_utc_ns,
            source=observation.origin.source,
            actor_name=observation.actor_name,
            actor_email=observation.actor_email,
        )

    def select(self, observations: Sequence[TimestampObservation]) -> tuple[TimestampObservation, ...]:
        """Return the observations matching this scope in their original order."""

        return tuple(observation for observation in observations if self.includes(observation))


def _matches_git_identity(
    actor_name: str | None,
    actor_email: str | None,
    filters: tuple[str, ...],
) -> bool:
    haystacks = tuple(value.casefold() for value in (actor_name, actor_email) if value is not None)
    return any(needle in haystack for needle in filters for haystack in haystacks)


__all__ = ["ObservationScope", "RefScope"]
