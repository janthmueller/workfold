"""User-supplied filesystem exclusion patterns."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from pathspec import GitIgnoreSpec

from workfold.collectors.ignores.models import ExclusionPatternError


@dataclass(frozen=True, slots=True)
class ExplicitExcluder:
    """Match the non-negating Git-wildmatch subset used by ``--exclude``."""

    patterns: tuple[str, ...]
    _spec: GitIgnoreSpec

    @classmethod
    def compile(cls, patterns: Sequence[str]) -> ExplicitExcluder:
        """Validate and compile repeatable exclusion patterns."""

        normalized = tuple(patterns)
        for pattern in normalized:
            if not pattern:
                raise ExclusionPatternError("explicit exclusion patterns cannot be empty")
            if pattern.startswith("!"):
                raise ExclusionPatternError(f"negated --exclude patterns are not supported: {pattern!r}")
            if "\0" in pattern:
                raise ExclusionPatternError("explicit exclusion patterns cannot contain NUL bytes")
        return cls(normalized, GitIgnoreSpec.from_lines(normalized))

    def matches(self, relative_path: PurePosixPath | str, *, is_directory: bool) -> bool:
        """Return whether a root-relative path is explicitly excluded."""

        value = relative_path.as_posix() if isinstance(relative_path, PurePosixPath) else relative_path
        value = value.lstrip("/")
        if not value or value == ".":
            return False
        if is_directory and not value.endswith("/"):
            value += "/"
        return self._spec.match_file(value)
