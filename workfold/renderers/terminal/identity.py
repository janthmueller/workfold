"""Deterministic terminal symbols for recorded Git identities."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from workfold.identities import MarkerIdentity, RecordedIdentity

_ASCII_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True, slots=True)
class IdentitySymbol:
    """One stable chart code and its full recorded identity group."""

    identity_id: int
    code: str
    identity: MarkerIdentity

    def marker(self, *, within_schedule: bool) -> str:
        """Return the schedule-aware form used by the chart."""

        if within_schedule:
            return self.code
        if self.code.startswith("◆"):
            return "◇" + self.code[1:]
        return self.code.lower()


def allocate_identity_symbols(identities: tuple[MarkerIdentity, ...]) -> tuple[IdentitySymbol, ...]:
    """Assign deterministic collision-free codes to an already sorted registry."""

    initials = {
        identity_id: _identity_initial(identity.members[0])
        for identity_id, identity in enumerate(identities)
        if len(identity.members) == 1
    }
    initial_counts = Counter(initials.values())
    initial_ordinals: Counter[str] = Counter()
    next_composite = 1
    symbols: list[IdentitySymbol] = []
    for identity_id, identity in enumerate(identities):
        if len(identity.members) > 1:
            code = "◆" if next_composite == 1 else f"◆{next_composite}"
            next_composite += 1
        else:
            initial = initials[identity_id]
            initial_ordinals[initial] += 1
            code = initial if initial_counts[initial] == 1 else f"{initial}{initial_ordinals[initial]}"
        symbols.append(IdentitySymbol(identity_id, code, identity))
    return tuple(symbols)


def recorded_identity_label(identity: RecordedIdentity) -> str:
    """Format one raw Git identity without asserting that it is verified."""

    if identity.name and identity.email:
        return f"{identity.name} <{identity.email}>"
    if identity.name:
        return identity.name
    if identity.email:
        return f"<{identity.email}>"
    return "(empty Git identity)"


def marker_identity_label(identity: MarkerIdentity) -> str:
    """Format every distinct identity retained by a possibly coalesced marker."""

    return " & ".join(recorded_identity_label(member) for member in identity.members)


def _identity_initial(identity: RecordedIdentity) -> str:
    for value in (identity.name, identity.email):
        word = _ASCII_WORD.search(_ascii(value))
        if word is not None:
            return word.group()[0].upper()
    return "I"


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


__all__ = [
    "IdentitySymbol",
    "allocate_identity_symbols",
    "marker_identity_label",
    "recorded_identity_label",
]
