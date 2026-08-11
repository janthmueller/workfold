"""Incremental observation selection and schedule classification."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias
from zoneinfo import ZoneInfo

from workfold.coverage import (
    PlottingDisposition,
    SelectionDisposition,
    TimestampCoverageKey,
)
from workfold.models import (
    ActivityMarker,
    ClassifiedMarker,
    RecordKind,
    Source,
    TimestampKind,
    TimestampObservation,
    coalesce_observations,
)
from workfold.schedule import Schedule, classify_marker
from workfold.time_ranges import InstantRangeUnion


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    """A validated, non-empty set of timestamps from one source record."""

    observations: tuple[TimestampObservation, ...]

    def __post_init__(self) -> None:
        if not self.observations:
            raise ValueError("an observation batch cannot be empty")
        origin_id = self.observations[0].origin.record_id
        if any(item.origin.record_id != origin_id for item in self.observations):
            raise ValueError("an observation batch must belong to one source record")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("an observation batch contains duplicate identities")

    @classmethod
    def create(cls, observations: Sequence[TimestampObservation]) -> ObservationBatch:
        return cls(tuple(observations))


ObservationConsumer: TypeAlias = Callable[[ObservationBatch], None]
ClassifiedMarkerConsumer: TypeAlias = Callable[[ClassifiedMarker], None]
SelectionCountKey: TypeAlias = tuple[TimestampCoverageKey, SelectionDisposition]
PlottingCountKey: TypeAlias = tuple[TimestampCoverageKey, PlottingDisposition]


class ActivityClassifier:
    """Select and classify record-local observations into a caller-owned sink."""

    def __init__(
        self,
        *,
        selected_range: InstantRangeUnion,
        identity_filters: tuple[str, ...],
        timezone_value: ZoneInfo,
        schedule: Schedule,
        marker_consumer: ClassifiedMarkerConsumer,
    ) -> None:
        self._selected_range = selected_range
        self._identity_filters = tuple(value.casefold() for value in identity_filters)
        self._timezone = timezone_value
        self._schedule = schedule
        self._marker_consumer = marker_consumer
        self._selection_counts: Counter[SelectionCountKey] = Counter()
        self._plotting_counts: Counter[PlottingCountKey] = Counter()
        self._coverage_keys: dict[tuple[Source, str, RecordKind, TimestampKind], TimestampCoverageKey] = {}

    @property
    def selection_counts(self) -> Mapping[SelectionCountKey, int]:
        return self._selection_counts

    @property
    def plotting_counts(self) -> Mapping[PlottingCountKey, int]:
        return self._plotting_counts

    def consume(self, batch: ObservationBatch) -> None:
        """Process one source record's observations and release its provenance."""

        observations = batch.observations

        included: list[TimestampObservation] = []
        coverage_keys: dict[str, TimestampCoverageKey] = {}
        for observation in observations:
            coverage_key = self._coverage_key(observation)
            coverage_keys[observation.observation_id] = coverage_key
            if not self._selected_range.contains(observation.instant_utc_ns):
                disposition = SelectionDisposition.OUTSIDE_DATE
            elif (
                self._identity_filters
                and observation.kind.source is Source.GIT
                and not _matches_git_identity(observation, self._identity_filters)
            ):
                disposition = SelectionDisposition.IDENTITY_FILTERED
            else:
                disposition = SelectionDisposition.INCLUDED
                included.append(observation)
            self._selection_counts[(coverage_key, disposition)] += 1

        markers = (
            tuple(ActivityMarker.create((observation,)) for observation in included)
            if observations[0].origin.source is Source.FILESYSTEM
            else coalesce_observations(included)
        )
        for marker in markers:
            for index, observation in enumerate(marker.observations):
                plotting = PlottingDisposition.MARKER if index == 0 else PlottingDisposition.COALESCED_INTO_MARKER
                self._plotting_counts[(coverage_keys[observation.observation_id], plotting)] += 1
            self._marker_consumer(classify_marker(marker, self._timezone, self._schedule))

    def _coverage_key(self, observation: TimestampObservation) -> TimestampCoverageKey:
        origin = observation.origin
        target = os.fspath(origin.repository_or_root)
        partition = (origin.source, target, origin.record_kind, observation.kind)
        key = self._coverage_keys.get(partition)
        if key is None:
            key = TimestampCoverageKey(
                origin.source,
                target,
                origin.record_kind,
                observation.kind,
            )
            self._coverage_keys[partition] = key
        return key


def _matches_git_identity(observation: TimestampObservation, filters: tuple[str, ...]) -> bool:
    haystacks = tuple(
        value.casefold() for value in (observation.actor_name, observation.actor_email) if value is not None
    )
    return any(needle in haystack for needle in filters for haystack in haystacks)


__all__ = [
    "ActivityClassifier",
    "ClassifiedMarkerConsumer",
    "ObservationBatch",
    "ObservationConsumer",
    "PlottingCountKey",
    "SelectionCountKey",
]
