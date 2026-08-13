"""Annotated and lightweight Git tag discovery."""

from workfold.collection.git.tags.collector import GitTagCollector
from workfold.collection.git.tags.models import (
    CollectedGitTag,
    DiscoveredGitTag,
    GitTagCollectionResult,
    GitTagRepositoryAccounting,
    ParsedTagObject,
)
from workfold.collection.git.tags.parser import GitTagParseError, parse_tag_object, parse_tag_refs

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
