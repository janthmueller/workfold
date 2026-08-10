"""Stable public facade for local Git tag collection."""

from workfold.collectors.tags.collector import GitTagCollector
from workfold.collectors.tags.models import (
    CollectedGitTag,
    DiscoveredGitTag,
    GitTagCollectionResult,
    GitTagRepositoryAccounting,
    ParsedTagObject,
)
from workfold.collectors.tags.parser import GitTagParseError, parse_tag_object, parse_tag_refs

__all__ = [
    "CollectedGitTag",
    "DiscoveredGitTag",
    "GitTagCollectionResult",
    "GitTagCollector",
    "GitTagParseError",
    "GitTagRepositoryAccounting",
    "ParsedTagObject",
    "parse_tag_object",
    "parse_tag_refs",
]
