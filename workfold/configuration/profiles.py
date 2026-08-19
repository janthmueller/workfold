"""Named event selections that leave discovery policy independent."""

from __future__ import annotations

from enum import Enum
from typing import assert_never

from workfold.domain.evidence import EvidenceKind, EvidenceSelection


class EventProfile(str, Enum):
    """One built-in shorthand for an exact event set."""

    GIT = "git"
    FILESYSTEM = "fs"
    BOTH = "both"
    PORTABLE = "portable"
    FULL = "full"


def evidence_for_profile(profile: EventProfile) -> EvidenceSelection:
    """Expand a profile without changing any independent discovery policy."""

    if profile is EventProfile.GIT:
        kinds = (EvidenceKind.GIT_COMMIT_AUTHOR,)
    elif profile is EventProfile.FILESYSTEM:
        kinds = (
            EvidenceKind.FS_FILE_BIRTH,
            EvidenceKind.FS_FILE_MODIFIED,
        )
    elif profile is EventProfile.BOTH:
        kinds = (
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.FS_FILE_BIRTH,
            EvidenceKind.FS_FILE_MODIFIED,
        )
    elif profile is EventProfile.PORTABLE:
        kinds = (
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.GIT_COMMIT_COMMITTER,
            EvidenceKind.GIT_TAG_TAGGER,
        )
    elif profile is EventProfile.FULL:
        kinds = tuple(EvidenceKind)
    else:
        assert_never(profile)
    return EvidenceSelection.create(kinds)


__all__ = ["EventProfile", "evidence_for_profile"]
