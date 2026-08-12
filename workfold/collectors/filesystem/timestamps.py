"""Filesystem timestamp extraction into normalized observations."""

from __future__ import annotations

import os

from workfold.collectors.base import CollectorDiagnostic
from workfold.collectors.filesystem.accounting import AccountingBuilder
from workfold.collectors.filesystem.types import FilesystemObservationConsumer, PendingEntry
from workfold.collectors.filesystem_times import FilesystemTimestampAdapter
from workfold.coverage import ExtractionDisposition
from workfold.models import TimestampKind, TimestampObservation
from workfold.provenance import observation_id
from workfold.scope import ObservationScope


def extract_entry(
    item: PendingEntry,
    kinds: tuple[TimestampKind, ...],
    *,
    adapter: FilesystemTimestampAdapter,
    accounting: AccountingBuilder,
    observations: list[TimestampObservation] | None,
    diagnostics: list[CollectorDiagnostic],
    observation_consumer: FilesystemObservationConsumer | None,
    observation_scope: ObservationScope | None,
) -> None:
    """Extract every requested timestamp slot from one stat-successful entry."""

    captured: list[TimestampObservation] = []
    for kind in kinds:
        accounting.request(item.root, kind)
        extraction = adapter.extract(item.snapshot, kind, path=item.path)
        if extraction.disposition is ExtractionDisposition.CAPTURED:
            if extraction.instant_utc_ns is None or extraction.raw_timestamp is None:
                raise RuntimeError("captured timestamp extraction omitted its value")
            accounting.extraction(item.root, kind, extraction.disposition)
            if observation_scope is not None and not observation_scope.includes_timestamp(
                instant_utc_ns=extraction.instant_utc_ns,
                source=kind.source,
            ):
                continue
            retained_id = observation_id(item.origin.record_id, kind.value) if observations is not None else None
            accounting.match_scope(item.root, kind, retained_id)
            observation = TimestampObservation.create(
                item.origin,
                kind,
                extraction.instant_utc_ns,
                extraction.raw_timestamp,
            )
            captured.append(observation)
            if observations is not None:
                observations.append(observation)
        else:
            accounting.extraction(item.root, kind, extraction.disposition)
            if extraction.disposition is ExtractionDisposition.ERROR:
                diagnostics.append(
                    CollectorDiagnostic(
                        code="filesystem_timestamp_error",
                        stage="filesystem_timestamp_extraction",
                        target=os.fspath(item.root),
                        message=extraction.note or "filesystem timestamp extraction failed",
                        path=os.fspath(item.path),
                        provenance_id=item.origin.record_id,
                    )
                )
    if captured and observation_consumer is not None:
        observation_consumer(tuple(captured))
