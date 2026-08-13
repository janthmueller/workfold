"""Incremental coalescing and schedule classification of selected observations."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from zoneinfo import ZoneInfo

from workfold.domain.coverage import (
    PlottingDisposition,
    TimestampCoverageKey,
)
from workfold.domain.observations import (
    ActivityMarker,
    ClassifiedMarker,
    RecordKind,
    Source,
    TimestampKind,
    TimestampObservation,
    coalesce_observations,
)
from workfold.domain.schedule import Schedule, classify_marker


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
ObservationCountKey: TypeAlias = TimestampCoverageKey
PlottingCountKey: TypeAlias = tuple[TimestampCoverageKey, PlottingDisposition]


class ActivityClassifier:
    """Coalesce and schedule-classify already selected observations."""

    def __init__(
        self,
        *,
        timezone_value: ZoneInfo,
        schedule: Schedule,
        marker_consumer: ClassifiedMarkerConsumer,
    ) -> None:
        self._timezone = timezone_value
        self._schedule = schedule
        self._marker_consumer = marker_consumer
        self._observation_counts: Counter[ObservationCountKey] = Counter()
        self._plotting_counts: Counter[PlottingCountKey] = Counter()
        self._coverage_keys: dict[tuple[Source, Path, RecordKind, TimestampKind], TimestampCoverageKey] = {}

    @property
    def observation_counts(self) -> Mapping[ObservationCountKey, int]:
        """Return selected observation counts by coverage partition."""

        return self._observation_counts

    @property
    def plotting_counts(self) -> Mapping[PlottingCountKey, int]:
        return self._plotting_counts

    def consume(self, batch: ObservationBatch) -> None:
        """Process one selected source record and release its provenance."""

        observations = batch.observations
        if observations[0].origin.source is Source.FILESYSTEM:
            for observation in observations:
                coverage_key = self._coverage_key(observation)
                self._observation_counts[coverage_key] += 1
                self._plotting_counts[(coverage_key, PlottingDisposition.MARKER)] += 1
                marker = ActivityMarker.create((observation,))
                self._marker_consumer(classify_marker(marker, self._timezone, self._schedule))
            return

        coverage_keys: dict[str, TimestampCoverageKey] = {}
        for observation in observations:
            coverage_key = self._coverage_key(observation)
            coverage_keys[observation.observation_id] = coverage_key
            self._observation_counts[coverage_key] += 1

        for marker in coalesce_observations(observations):
            for index, observation in enumerate(marker.observations):
                plotting = PlottingDisposition.MARKER if index == 0 else PlottingDisposition.COALESCED_INTO_MARKER
                self._plotting_counts[(coverage_keys[observation.observation_id], plotting)] += 1
            self._marker_consumer(classify_marker(marker, self._timezone, self._schedule))

    def _coverage_key(self, observation: TimestampObservation) -> TimestampCoverageKey:
        origin = observation.origin
        partition = (origin.source, origin.repository_or_root, origin.record_kind, observation.kind)
        key = self._coverage_keys.get(partition)
        if key is None:
            key = TimestampCoverageKey(
                origin.source,
                os.fspath(origin.repository_or_root),
                origin.record_kind,
                observation.kind,
            )
            self._coverage_keys[partition] = key
        return key


__all__ = [
    "ActivityClassifier",
    "ClassifiedMarkerConsumer",
    "ObservationCountKey",
    "ObservationBatch",
    "ObservationConsumer",
    "PlottingCountKey",
]
