"""Incremental observation selection, classification, and chart aggregation."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import TypeAlias
from zoneinfo import ZoneInfo

from workfold.aggregation import Aggregation, AggregationBuilder
from workfold.coverage import (
    PlottingDisposition,
    SelectionDisposition,
    TimestampCoverageKey,
)
from workfold.models import Source, TimestampObservation, coalesce_observations
from workfold.schedule import Schedule, classify_marker
from workfold.time_ranges import InstantRangeUnion

ObservationConsumer: TypeAlias = Callable[[Sequence[TimestampObservation]], None]
SelectionCountKey: TypeAlias = tuple[TimestampCoverageKey, SelectionDisposition]
PlottingCountKey: TypeAlias = tuple[TimestampCoverageKey, PlottingDisposition]


class ActivityPipeline:
    """Consume record-local observations without retaining the full inventory."""

    def __init__(
        self,
        *,
        selected_range: InstantRangeUnion,
        identity_filters: tuple[str, ...],
        timezone_value: ZoneInfo,
        schedule: Schedule,
        cluster_window: timedelta,
        display_range: tuple[int, int] | None,
        outside_limit: int,
    ) -> None:
        self._selected_range = selected_range
        self._identity_filters = tuple(value.casefold() for value in identity_filters)
        self._timezone = timezone_value
        self._schedule = schedule
        self._selection_counts: Counter[SelectionCountKey] = Counter()
        self._plotting_counts: Counter[PlottingCountKey] = Counter()
        self._aggregation = AggregationBuilder(
            cluster_window=cluster_window,
            schedule_bounds=schedule.bounds,
            display_range=display_range,
            outside_limit=outside_limit,
        )

    @property
    def selection_counts(self) -> Mapping[SelectionCountKey, int]:
        return self._selection_counts

    @property
    def plotting_counts(self) -> Mapping[PlottingCountKey, int]:
        return self._plotting_counts

    def consume(self, observations: Sequence[TimestampObservation]) -> None:
        """Process one source record's observations and release its provenance."""

        batch = tuple(observations)
        if not batch:
            return
        origin_id = batch[0].origin.record_id
        if any(item.origin.record_id != origin_id for item in batch):
            raise RuntimeError("an observation batch must belong to one source record")
        if len({item.observation_id for item in batch}) != len(batch):
            raise RuntimeError("an observation batch contains duplicate identities")

        included: list[TimestampObservation] = []
        for observation in batch:
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
            self._selection_counts[(_coverage_key(observation), disposition)] += 1

        for marker in coalesce_observations(included):
            for index, observation in enumerate(marker.observations):
                plotting = PlottingDisposition.MARKER if index == 0 else PlottingDisposition.COALESCED_INTO_MARKER
                self._plotting_counts[(_coverage_key(observation), plotting)] += 1
            self._aggregation.add(classify_marker(marker, self._timezone, self._schedule))

    def build(self) -> Aggregation:
        """Finish the bounded chart aggregation."""

        return self._aggregation.build()


def _coverage_key(observation: TimestampObservation) -> TimestampCoverageKey:
    origin = observation.origin
    return TimestampCoverageKey(
        origin.source,
        os.fspath(origin.repository_or_root),
        origin.record_kind,
        observation.kind,
    )


def _matches_git_identity(observation: TimestampObservation, filters: tuple[str, ...]) -> bool:
    haystacks = tuple(
        value.casefold() for value in (observation.actor_name, observation.actor_email) if value is not None
    )
    return any(needle in haystack for needle in filters for haystack in haystacks)


__all__ = [
    "ActivityPipeline",
    "ObservationConsumer",
    "PlottingCountKey",
    "SelectionCountKey",
]
