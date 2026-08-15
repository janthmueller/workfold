"""Coordinate enabled evidence sources and merge their bounded batches."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from workfold.application.collection_plan import CollectionPlan
from workfold.collection.diagnostics import CollectorDiagnostic, DiagnosticSeverity
from workfold.collection.filesystem import (
    FilesystemCollectionResult,
    FilesystemCollectionSummary,
    FilesystemCollector,
)
from workfold.collection.git.evidence import (
    GitEvidenceCollectionResult,
    GitEvidenceCollector,
    GitEvidenceRequest,
    GitEvidenceSummary,
)
from workfold.configuration.options import RunOptions, UsageError
from workfold.domain.coverage import Capability, CoverageFragment
from workfold.domain.observations import Source, TimestampObservation
from workfold.domain.scope import ObservationScope
from workfold.folding.pipeline import ObservationBatch, ObservationConsumer


@dataclass(frozen=True, slots=True)
class Collection:
    """Results and accounting emitted by one application-level collection pass."""

    diagnostics: tuple[CollectorDiagnostic, ...]
    capabilities: tuple[Capability, ...]
    any_collector_succeeded: bool
    git_summary: GitEvidenceSummary | None = None
    filesystem_summary: FilesystemCollectionSummary | None = None
    coverage_fragments: tuple[CoverageFragment, ...] = ()

    @property
    def diagnostic_counts(self) -> tuple[int, int, int]:
        """Return exact error, warning, and informational diagnostic counts."""

        errors = sum(item.occurrence_count(DiagnosticSeverity.ERROR) for item in self.diagnostics)
        warnings = sum(item.occurrence_count(DiagnosticSeverity.WARNING) for item in self.diagnostics)
        infos = sum(item.occurrence_count(DiagnosticSeverity.INFO) for item in self.diagnostics)
        return errors, warnings, infos


@dataclass(frozen=True, slots=True)
class CollectorServices:
    """Source-level collectors supplied by the outer composition boundary."""

    git: GitEvidenceCollector
    filesystem: FilesystemCollector


def collect(
    options: RunOptions,
    collectors: CollectorServices,
    *,
    observation_consumer: ObservationConsumer,
    observation_scope: ObservationScope | None,
) -> Collection:
    """Run every enabled source and retain only aggregate accounting."""

    plan = CollectionPlan.from_selection(options.evidence)
    diagnostics: list[CollectorDiagnostic] = []
    capabilities: list[Capability] = []
    coverage_fragments: list[CoverageFragment] = []
    any_succeeded = False
    git_result: GitEvidenceCollectionResult | None = None
    filesystem_result: FilesystemCollectionResult | None = None

    def deliver(observations: Sequence[TimestampObservation]) -> None:
        if observations:
            observation_consumer(ObservationBatch.create(observations))

    if plan.includes_git:
        git_result = collectors.git.collect(
            GitEvidenceRequest(
                paths=options.paths,
                ref_scope=options.ref_scope,
                commit_timestamps=plan.commit_timestamps,
                file_change_timestamps=plan.file_change_timestamps,
                collect_tags=plan.collect_tags,
                collect_reflogs=plan.collect_reflogs,
                observation_scope=observation_scope,
            ),
            observation_consumer=deliver,
        )
        diagnostics.extend(git_result.diagnostics)
        coverage_fragments.append(git_result.coverage)
        any_succeeded |= git_result.successful

    if plan.includes_filesystem:
        try:
            filesystem_result = collectors.filesystem.collect(
                options.paths,
                entry_timestamps=plan.filesystem_timestamps,
                respect_gitignore=options.respect_gitignore,
                include_ignored=options.include_ignored,
                exclusions=options.exclusions,
                # The filesystem collector applies this exact scope before it
                # materializes and emits observations. Deliver its batches
                # directly instead of evaluating every timestamp twice.
                observation_consumer=deliver,
                observation_scope=(
                    observation_scope
                    if observation_scope is not None and observation_scope.is_restrictive_for(Source.FILESYSTEM)
                    else None
                ),
                retain_entries=False,
                retain_observations=False,
            )
        except ValueError as error:
            raise UsageError(str(error)) from error
        diagnostics.extend(filesystem_result.diagnostics)
        capabilities.extend(filesystem_result.capabilities)
        coverage_fragments.append(filesystem_result.coverage_fragment())
        any_succeeded |= bool(filesystem_result.successful_roots)

    return Collection(
        diagnostics=tuple(diagnostics),
        capabilities=tuple(capabilities),
        any_collector_succeeded=any_succeeded,
        git_summary=git_result.summary if git_result is not None else None,
        filesystem_summary=filesystem_result.summary if filesystem_result is not None else None,
        coverage_fragments=tuple(coverage_fragments),
    )


__all__ = ["Collection", "CollectorServices", "collect"]
