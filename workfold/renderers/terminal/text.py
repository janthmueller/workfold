"""Width-aware terminal text composition helpers."""

from __future__ import annotations

from rich.text import Text

from workfold.sanitization import display_width, pad_right, sanitize_terminal_text, truncate_end


def center(value: str, width: int) -> str:
    fitted = truncate_end(value, width)
    padding = max(0, width - display_width(fitted))
    left = padding // 2
    return " " * left + fitted + " " * (padding - left)


def plain_section(value: str, *, heading: bool = False) -> tuple[Text, ...]:
    lines = value.splitlines()
    return tuple(Text(line, style="bold") if heading and index == 0 else Text(line) for index, line in enumerate(lines))


def aligned_fact_lines(facts: list[tuple[str, str]], width: int) -> list[str]:
    if not facts:
        return []
    safe_facts = [(sanitize_terminal_text(label), sanitize_terminal_text(value)) for label, value in facts]
    label_width = max(display_width(label) for label, _value in safe_facts)
    lines: list[str] = []
    for label, value in safe_facts:
        prefix = f"{pad_right(label, label_width)}  "
        available = max(1, width - display_width(prefix))
        chunks = column_chunks(value, available)
        if not chunks:
            lines.append(prefix.rstrip())
            continue
        indent = " " * display_width(prefix)
        lines.extend((prefix + chunks[0], *(indent + chunk for chunk in chunks[1:])))
    return lines


def fact_lines(label: str, value: object, width: int) -> list[str]:
    safe_label = sanitize_terminal_text(label)
    safe_value = sanitize_terminal_text(value)
    prefix = f"{safe_label}: "
    available = max(1, width - display_width(prefix))
    chunks = column_chunks(safe_value, available)
    if not chunks:
        return [prefix]
    indent = " " * display_width(prefix)
    return [prefix + chunks[0], *(indent + chunk for chunk in chunks[1:])]


def column_chunks(text: str, width: int) -> list[str]:
    if not text:
        return []
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current = ""
    for word in words:
        if display_width(word) > width:
            if current:
                chunks.append(current)
                current = ""
            hard_chunks = _hard_column_chunks(word, width)
            chunks.extend(hard_chunks[:-1])
            current = hard_chunks[-1]
            continue

        candidate = word if not current else f"{current} {word}"
        if display_width(candidate) <= width:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def fit_plain(line: str, width: int) -> str:
    return truncate_end(line, width)


def rich_text_chunks(value: Text, width: int) -> tuple[Text, ...]:
    """Hard-fold styled text without dropping content or Rich spans."""

    if width < 1:
        raise ValueError("text width must be positive")
    if not value:
        return ()
    chunks: list[Text] = []
    start = 0
    used = 0
    for index, character in enumerate(value.plain):
        character_width = display_width(character)
        if index > start and used + character_width > width:
            chunks.append(value[start:index])
            start = index
            used = 0
        used += character_width
    chunks.append(value[start:])
    return tuple(chunks)


def _hard_column_chunks(text: str, width: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0
    for character in text:
        character_width = display_width(character)
        if current and current_width + character_width > width:
            chunks.append("".join(current))
            current = []
            current_width = 0
        current.append(character)
        current_width += character_width
    if current:
        chunks.append("".join(current))
    return chunks
