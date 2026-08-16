from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest
import workfold.collection.filesystem.timestamps as timestamp_collection
from workfold.collection.filesystem.accounting import AccountingBuilder
from workfold.collection.filesystem.metadata import FilesystemTimestampAdapter
from workfold.collection.filesystem.scan import PendingEntry
from workfold.domain.coverage import ExtractionDisposition, RecordDisposition
from workfold.domain.observations import EntryType, TimestampKind
from workfold.domain.scope import ObservationScope
from workfold.domain.time import InstantRange, InstantRangeUnion


def test_out_of_scope_timestamp_does_not_materialize_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    path = root / "file.txt"
    snapshot = cast(os.stat_result, SimpleNamespace(st_mtime_ns=1_000_000_000))
    item = PendingEntry(root, path, snapshot, None, EntryType.REGULAR_FILE)
    accounting = AccountingBuilder(retain_scope_match_ids=False)
    accounting.ensure_root(root, {EntryType.REGULAR_FILE: (TimestampKind.FS_MODIFIED,)})
    accounting.discover(root)
    accounting.record(root, RecordDisposition.ELIGIBLE)
    scope = ObservationScope(InstantRangeUnion((InstantRange(2_000_000_000, 3_000_000_000),)))

    def fail_if_materialized(_item: PendingEntry) -> NoReturn:
        raise AssertionError("out-of-scope filesystem provenance was materialized")

    monkeypatch.setattr(timestamp_collection, "pending_origin", fail_if_materialized)

    timestamp_collection.extract_entry(
        item,
        (TimestampKind.FS_MODIFIED,),
        adapter=FilesystemTimestampAdapter(),
        accounting=accounting,
        observations=None,
        diagnostics=[],
        observation_consumer=None,
        observation_scope=scope,
    )

    extraction = accounting.build().timestamps[0]
    assert extraction.requested == extraction.captured == 1
    assert extraction.scope_matches == 0


def test_accounting_canonicalizes_scope_match_identity_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    builder = AccountingBuilder()
    selection = {EntryType.REGULAR_FILE: (TimestampKind.FS_MODIFIED,)}
    builder.ensure_root(root, selection)
    builder.discover(root, 2)
    builder.record(root, RecordDisposition.ELIGIBLE, 2)
    for observation_id in ("z-observation", "a-observation"):
        builder.request(root, EntryType.REGULAR_FILE, TimestampKind.FS_MODIFIED)
        builder.extraction(
            root,
            EntryType.REGULAR_FILE,
            TimestampKind.FS_MODIFIED,
            ExtractionDisposition.CAPTURED,
        )
        builder.match_scope(
            root,
            EntryType.REGULAR_FILE,
            TimestampKind.FS_MODIFIED,
            observation_id,
        )

    assert builder.build().timestamps[0].scope_match_ids == ("a-observation", "z-observation")
