"""Filesystem exclusion patterns and standard Git ignore integration."""

from workfold.collection.filesystem.ignore.exclusions import ExplicitExcluder
from workfold.collection.filesystem.ignore.models import (
    ExclusionPatternError,
    GitFilesystemInventory,
    GitFilesystemInventoryView,
    GitFilesystemInventoryVisit,
    GitIgnoreCommandError,
    GitIgnoreMatches,
    GitIgnoreProbe,
    GitIgnoreRepository,
    IgnoreCandidate,
    InventoryStrategy,
)
from workfold.collection.filesystem.ignore.paths import (
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_git_admin_name,
    is_git_admin_path,
    is_nested_repository_boundary,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.collection.filesystem.ignore.runner import GitIgnoreRunner
from workfold.collection.filesystem.ignore.service import GitIgnoreService

__all__ = [
    "ExclusionPatternError",
    "ExplicitExcluder",
    "GitFilesystemInventory",
    "GitFilesystemInventoryView",
    "GitFilesystemInventoryVisit",
    "GitIgnoreCommandError",
    "GitIgnoreMatches",
    "GitIgnoreProbe",
    "GitIgnoreRepository",
    "GitIgnoreRunner",
    "GitIgnoreService",
    "IgnoreCandidate",
    "InventoryStrategy",
    "has_git_admin_ancestor",
    "has_repository_marker_ancestor",
    "is_git_admin_name",
    "is_git_admin_path",
    "is_nested_repository_boundary",
    "is_within_git_admin",
    "looks_like_bare_repository",
]
