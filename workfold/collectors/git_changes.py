"""Compatibility façade for atomic Git file-change collection."""

from workfold.collectors.git_core.change_collector import GitFileChangeCollector
from workfold.collectors.git_core.change_models import (
    CollectedGitFileChange,
    GitFileChangeCollectionResult,
    GitFileChangeRepositoryAccounting,
)
from workfold.collectors.git_core.diff_tree import (
    GitChangeParseError,
    ParsedGitChange,
    parse_diff_tree_name_status,
)

__all__ = [
    "CollectedGitFileChange",
    "GitChangeParseError",
    "GitFileChangeCollectionResult",
    "GitFileChangeCollector",
    "GitFileChangeRepositoryAccounting",
    "ParsedGitChange",
    "parse_diff_tree_name_status",
]
