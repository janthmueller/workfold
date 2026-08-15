"""Public source boundary for all supported local Git evidence."""

from workfold.collection.git.evidence.collector import GitEvidenceCollector, merge_file_change_results
from workfold.collection.git.evidence.models import (
    GitCommitInputSummary,
    GitCommitInputTargetSummary,
    GitEvidenceCollectionResult,
    GitEvidenceRequest,
    GitEvidenceSummary,
    GitFileChangeSummary,
    GitFileChangeTargetSummary,
    GitObservationConsumer,
    GitReflogSummary,
    GitTagSummary,
)

__all__ = [
    "GitCommitInputSummary",
    "GitCommitInputTargetSummary",
    "GitEvidenceCollectionResult",
    "GitEvidenceCollector",
    "GitEvidenceRequest",
    "GitEvidenceSummary",
    "GitFileChangeSummary",
    "GitFileChangeTargetSummary",
    "GitObservationConsumer",
    "GitReflogSummary",
    "GitTagSummary",
    "merge_file_change_results",
]
