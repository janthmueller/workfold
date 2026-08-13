"""Exact Git revision and raw-object parsing."""

from workfold.collection.git.objects.cat_file import parse_cat_file_batch, read_cat_file_batch_record
from workfold.collection.git.objects.commit_parser import parse_commit_object
from workfold.collection.git.objects.compact import (
    read_cat_file_batch_commit,
    read_cat_file_batch_compact_object,
)
from workfold.collection.git.objects.models import (
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
from workfold.collection.git.objects.revision_scan import inspect_rev_list_scan
from workfold.collection.git.objects.signatures import parse_git_signature

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
