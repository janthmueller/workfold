"""Finalize source-owned coverage with shared pipeline outcomes."""

from __future__ import annotations

from collections.abc import Mapping

from workfold.application.collection import Collection
from workfold.domain.coverage import CoverageLedger, finalize_coverage_fragments
from workfold.folding.pipeline import ObservationCountKey, PlottingCountKey


def build_coverage(
    collection: Collection,
    *,
    observations: Mapping[ObservationCountKey, int],
    plotting: Mapping[PlottingCountKey, int],
) -> CoverageLedger:
    """Reconcile source accounting with independently measured pipeline outcomes."""

    return finalize_coverage_fragments(collection.coverage_fragments, observations, plotting)


__all__ = ["build_coverage"]
