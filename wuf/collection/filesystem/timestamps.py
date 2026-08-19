"""Filesystem timestamp extraction into normalized observations."""

from __future__ import annotations

import os

from wuf.collection.diagnostics import CollectorDiagnostic, DiagnosticSink
from wuf.collection.filesystem.accounting import AccountingBuilder
from wuf.collection.filesystem.entries import pending_origin
from wuf.collection.filesystem.metadata import FilesystemTimestampAdapter
from wuf.collection.filesystem.scan import FilesystemObservationConsumer, PendingEntry
from wuf.domain.coverage import ExtractionDisposition
from wuf.domain.observations import RecordOrigin, TimestampKind, TimestampObservation
from wuf.domain.provenance import observation_id
from wuf.domain.scope import ObservationScope


def extract_entry(
    item: PendingEntry,
    kinds: tuple[TimestampKind, ...],
    *,
    adapter: FilesystemTimestampAdapter,
    accounting: AccountingBuilder,
    observations: list[TimestampObservation] | None,
    diagnostics: DiagnosticSink,
    observation_consumer: FilesystemObservationConsumer | None,
    observation_scope: ObservationScope | None,
) -> None:
    """Extract every requested timestamp slot from one stat-successful entry."""

    captured: list[TimestampObservation] = []
    resolved_origin = item.origin
    if item.entry_type is None:
        raise ValueError("timestamp extraction requires a supported filesystem entry type")

    def resolve_origin() -> RecordOrigin:
        nonlocal resolved_origin
        if resolved_origin is None:
            resolved_origin = pending_origin(item)
        return resolved_origin

    for kind in kinds:
        accounting.request(item.root, item.entry_type, kind)
        extraction = adapter.extract(item.snapshot, kind, path=item.path)
        if extraction.disposition is ExtractionDisposition.CAPTURED:
            if extraction.instant_utc_ns is None or extraction.raw_timestamp is None:
                raise RuntimeError("captured timestamp extraction omitted its value")
            accounting.extraction(item.root, item.entry_type, kind, extraction.disposition)
            if observation_scope is not None and not observation_scope.includes_timestamp(
                instant_utc_ns=extraction.instant_utc_ns,
                source=kind.source,
            ):
                continue
            entry_origin = resolve_origin()
            retained_id = observation_id(entry_origin.record_id, kind.value) if observations is not None else None
            accounting.match_scope(item.root, item.entry_type, kind, retained_id)
            observation = TimestampObservation.create(
                entry_origin,
                kind,
                extraction.instant_utc_ns,
                extraction.raw_timestamp,
            )
            captured.append(observation)
            if observations is not None:
                observations.append(observation)
        else:
            accounting.extraction(item.root, item.entry_type, kind, extraction.disposition)
            if extraction.disposition is ExtractionDisposition.ERROR:
                entry_origin = resolve_origin()
                diagnostics.append(
                    CollectorDiagnostic(
                        code="filesystem_timestamp_error",
                        stage="filesystem_timestamp_extraction",
                        target=os.fspath(item.root),
                        message=extraction.note or "filesystem timestamp extraction failed",
                        path=os.fspath(item.path),
                        provenance_id=entry_origin.record_id,
                    )
                )
    if captured and observation_consumer is not None:
        observation_consumer(tuple(captured))
