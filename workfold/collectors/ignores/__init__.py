"""Filesystem exclusion patterns and standard Git ignore integration."""

from workfold.collectors.ignores.exclusions import ExplicitExcluder
from workfold.collectors.ignores.models import (
    ExclusionPatternError,
    GitFilesystemInventory,
    GitFilesystemInventoryVisit,
    GitIgnoreCommandError,
    GitIgnoreMatches,
    GitIgnoreProbe,
    GitIgnoreRepository,
    IgnoreCandidate,
)
from workfold.collectors.ignores.paths import (
    has_git_admin_ancestor,
    has_repository_marker_ancestor,
    is_git_admin_name,
    is_git_admin_path,
    is_nested_repository_boundary,
    is_within_git_admin,
    looks_like_bare_repository,
)
from workfold.collectors.ignores.runner import GitIgnoreRunner
from workfold.collectors.ignores.service import GitIgnoreService

__all__ = [
    "ExclusionPatternError",
    "ExplicitExcluder",
    "GitFilesystemInventory",
    "GitFilesystemInventoryVisit",
    "GitIgnoreCommandError",
    "GitIgnoreMatches",
    "GitIgnoreProbe",
    "GitIgnoreRepository",
    "GitIgnoreRunner",
    "GitIgnoreService",
    "IgnoreCandidate",
    "has_git_admin_ancestor",
    "has_repository_marker_ancestor",
    "is_git_admin_name",
    "is_git_admin_path",
    "is_nested_repository_boundary",
    "is_within_git_admin",
    "looks_like_bare_repository",
]
