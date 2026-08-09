"""Deterministic identities for collected records and timestamp observations.

The functions in this module deliberately avoid :func:`hash`: Python hashes are
salted per process and are therefore unsuitable for provenance.  Inputs are
encoded as typed, length-delimited fields before being passed to BLAKE2b, which
also prevents ambiguous concatenations such as ``("ab", "c")`` and
``("a", "bc")`` from colliding at the encoding layer.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePath
from typing import TypeAlias

CanonicalPart: TypeAlias = str | bytes | int | PurePath | None

_DIGEST_SIZE = 32


def lexical_absolute(path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None) -> Path:
    """Return a lexical absolute path without resolving symlink targets."""

    raw_path = os.fspath(path)
    if base is not None and not os.path.isabs(raw_path):
        raw_path = os.path.join(os.fspath(base), raw_path)
    return Path(os.path.abspath(raw_path))


def canonical_bytes(namespace: str, *parts: CanonicalPart) -> bytes:
    """Encode a domain namespace and values into unambiguous canonical bytes."""

    encoded = bytearray(b"workfold-id-v1\x00")
    for part in (namespace, *parts):
        tag, payload = _encode_part(part)
        encoded.extend(tag)
        encoded.extend(len(payload).to_bytes(8, byteorder="big", signed=False))
        encoded.extend(payload)
    return bytes(encoded)


def canonical_id(namespace: str, *parts: CanonicalPart) -> str:
    """Return a stable, domain-separated hexadecimal identity."""

    digest = hashlib.blake2b(canonical_bytes(namespace, *parts), digest_size=_DIGEST_SIZE)
    return digest.hexdigest()


def repository_id(repository: str | os.PathLike[str]) -> str:
    """Identify a repository by its lexical absolute location for this run."""

    return canonical_id("repository", lexical_absolute(repository))


def git_commit_id(repository: str | os.PathLike[str], commit_oid: str) -> str:
    """Identify one Git commit record within a repository."""

    return canonical_id("git-commit", lexical_absolute(repository), commit_oid)


def git_file_change_id(
    repository: str | os.PathLike[str],
    commit_oid: str,
    diff_basis: str,
    status: str,
    old_path: str | None,
    new_path: str | None,
) -> str:
    """Identify one file change relative to its exact diff basis."""

    return canonical_id(
        "git-file-change",
        lexical_absolute(repository),
        commit_oid,
        diff_basis,
        status,
        old_path,
        new_path,
    )


def git_tag_id(
    repository: str | os.PathLike[str],
    ref_name: str,
    tag_object_oid: str | None,
    target_oid: str,
) -> str:
    """Identify an annotated or lightweight Git tag record."""

    return canonical_id("git-tag", lexical_absolute(repository), ref_name, tag_object_oid, target_oid)


def git_reflog_id(
    repository: str | os.PathLike[str],
    ref_name: str,
    old_oid: str,
    new_oid: str,
    raw_selector: str,
    raw_timestamp: str,
    actor: str,
    message: str,
    duplicate_ordinal: int,
) -> str:
    """Identify a reflog entry, including a deterministic duplicate ordinal."""

    if duplicate_ordinal < 0:
        raise ValueError("duplicate ordinal must be non-negative")
    return canonical_id(
        "git-reflog",
        lexical_absolute(repository),
        ref_name,
        old_oid,
        new_oid,
        raw_selector,
        raw_timestamp,
        actor,
        message,
        duplicate_ordinal,
    )


def filesystem_entry_id(
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    entry_type: str,
) -> str:
    """Identify a filesystem entry without resolving a symlink target."""

    absolute_root = lexical_absolute(root)
    absolute_path = lexical_absolute(path, base=absolute_root)
    return canonical_id("filesystem-entry", absolute_root, absolute_path, entry_type)


def timestamp_slot_id(record_id: str, timestamp_kind: str) -> str:
    """Identify a requested timestamp slot and its eventual observation."""

    return canonical_id("timestamp-slot", record_id, timestamp_kind)


def observation_id(record_id: str, timestamp_kind: str) -> str:
    """Identify the observation captured from a timestamp slot."""

    return timestamp_slot_id(record_id, timestamp_kind)


def activity_marker_id(observation_ids: tuple[str, ...]) -> str:
    """Identify a marker from a canonical ordering of observation identities."""

    if not observation_ids:
        raise ValueError("an activity marker needs at least one observation")
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("an activity marker cannot contain duplicate observations")
    return canonical_id("activity-marker", *sorted(observation_ids))


def _encode_part(part: object) -> tuple[bytes, bytes]:
    if part is None:
        return b"n", b""
    if isinstance(part, bytes):
        return b"b", part
    if isinstance(part, PurePath):
        return b"p", os.fspath(part).encode("utf-8", errors="surrogatepass")
    if isinstance(part, str):
        return b"s", part.encode("utf-8", errors="surrogatepass")
    if isinstance(part, bool):
        raise TypeError("unsupported canonical identity part: bool")
    if isinstance(part, int):
        return b"i", str(part).encode("ascii")
    raise TypeError(f"unsupported canonical identity part: {type(part).__name__}")


__all__ = [
    "CanonicalPart",
    "activity_marker_id",
    "canonical_bytes",
    "canonical_id",
    "filesystem_entry_id",
    "git_commit_id",
    "git_file_change_id",
    "git_reflog_id",
    "git_tag_id",
    "lexical_absolute",
    "observation_id",
    "repository_id",
    "timestamp_slot_id",
]
