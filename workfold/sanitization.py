"""Helpers for rendering untrusted text safely in a terminal.

Git metadata and filesystem paths are user-controlled input.  Renderers must
pass them through this module before writing them to a terminal so embedded
control sequences cannot move the cursor, recolor subsequent output, or forge
additional report rows.
"""

from __future__ import annotations

import unicodedata


def sanitize_terminal_text(value: object) -> str:
    """Return printable, single-line text with control characters escaped.

    Printable Unicode is retained.  C0/C1 controls, DEL, format controls (which
    include bidi overrides), surrogates, and other non-printing code points are
    represented explicitly.  Escaping rather than dropping them preserves
    evidence that the source value contained unusual data.
    """

    text = str(value)
    safe: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category[0] != "C" and category not in {"Zl", "Zp"}:
            safe.append(character)
            continue

        codepoint = ord(character)
        common_escape = {
            "\0": r"\0",
            "\a": r"\a",
            "\b": r"\b",
            "\t": r"\t",
            "\n": r"\n",
            "\v": r"\v",
            "\f": r"\f",
            "\r": r"\r",
            "\x1b": r"\x1b",
        }.get(character)
        if common_escape is not None:
            safe.append(common_escape)
        elif codepoint <= 0xFF:
            safe.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            safe.append(f"\\u{codepoint:04x}")
        else:
            safe.append(f"\\U{codepoint:08x}")
    return "".join(safe)


def display_width(text: str) -> int:
    """Return an approximation of the number of terminal columns in *text*.

    This deliberately handles only plain text.  ANSI is never accepted here:
    generated styling is added after layout, and untrusted ANSI is escaped by
    :func:`sanitize_terminal_text`.
    """

    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def truncate_end(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """Truncate *text* to at most *max_width* terminal columns."""

    if max_width < 0:
        raise ValueError("max_width must not be negative")
    if display_width(text) <= max_width:
        return text
    if max_width == 0:
        return ""

    ellipsis_width = display_width(ellipsis)
    if ellipsis_width > max_width:
        return _take_prefix(ellipsis, max_width)
    return _take_prefix(text, max_width - ellipsis_width) + ellipsis


def truncate_middle(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """Middle-ellipsize *text* to at most *max_width* terminal columns."""

    if max_width < 0:
        raise ValueError("max_width must not be negative")
    if display_width(text) <= max_width:
        return text
    if max_width == 0:
        return ""

    ellipsis_width = display_width(ellipsis)
    if ellipsis_width > max_width:
        return _take_prefix(ellipsis, max_width)

    remaining = max_width - ellipsis_width
    left_width = (remaining + 1) // 2
    right_width = remaining - left_width
    return _take_prefix(text, left_width) + ellipsis + _take_suffix(text, right_width)


def pad_right(text: str, width: int) -> str:
    """Pad plain *text* to exactly *width* columns, truncating if necessary."""

    fitted = truncate_end(text, width)
    return fitted + " " * (width - display_width(fitted))


def _take_prefix(text: str, width: int) -> str:
    result: list[str] = []
    used = 0
    for character in text:
        character_width = (
            0
            if unicodedata.combining(character)
            else (2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1)
        )
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(result)


def _take_suffix(text: str, width: int) -> str:
    result: list[str] = []
    used = 0
    for character in reversed(text):
        character_width = (
            0
            if unicodedata.combining(character)
            else (2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1)
        )
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
    result.reverse()
    return "".join(result)
