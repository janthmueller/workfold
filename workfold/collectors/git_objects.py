"""Convenience façade for exact Git revision and raw-object parsing.

Implementation lives in focused modules under :mod:`workfold.collectors.git_core`.
Collector internals depend on those focused modules directly; this module keeps
the current extension and test import surface compact without promising legacy
parser signatures during the alpha API cycle.
"""

from workfold.collectors.git_core.cat_file import parse_cat_file_batch, read_cat_file_batch_record
from workfold.collectors.git_core.commit_parser import parse_commit_object
from workfold.collectors.git_core.compact_object import (
    read_cat_file_batch_commit,
    read_cat_file_batch_compact_object,
)
from workfold.collectors.git_core.object_model import (
    BatchObject,
    BatchObjectResult,
    BatchParseResult,
    CommitBatchResult,
    CompactBatchObject,
    CompactBatchResult,
    GitIdentity,
    GitObjectParseError,
    GitSignature,
    GitSignatureRole,
    InvalidBatchCommit,
    InvalidBatchObject,
    ParsedCommit,
    RevListCommitScan,
    RevListScanSpec,
    UnavailableBatchObject,
    UnexpectedBatchObject,
)
from workfold.collectors.git_core.revision_scan import inspect_rev_list_scan
from workfold.collectors.git_core.signatures import parse_git_signature

__all__ = [
    "BatchObject",
    "BatchObjectResult",
    "BatchParseResult",
    "CompactBatchObject",
    "CompactBatchResult",
    "CommitBatchResult",
    "GitIdentity",
    "GitObjectParseError",
    "GitSignature",
    "GitSignatureRole",
    "InvalidBatchCommit",
    "InvalidBatchObject",
    "ParsedCommit",
    "RevListCommitScan",
    "RevListScanSpec",
    "UnavailableBatchObject",
    "UnexpectedBatchObject",
    "inspect_rev_list_scan",
    "parse_cat_file_batch",
    "parse_commit_object",
    "parse_git_signature",
    "read_cat_file_batch_commit",
    "read_cat_file_batch_compact_object",
    "read_cat_file_batch_record",
]
