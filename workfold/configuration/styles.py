"""Validated event-style rules compiled against canonical evidence signatures."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast

from rich.cells import split_graphemes
from rich.color import Color, ColorParseError

from workfold.domain.evidence import (
    EvidenceKind,
    evidence_kinds_from_mask,
    evidence_mask_source,
    expand_evidence_selectors,
    supported_marker_evidence_masks,
)
from workfold.domain.observations import Source

STYLE_KEYS = frozenset({"symbol", "color", "outside-symbol", "outside-color"})


@dataclass(frozen=True, slots=True)
class MarkerVisual:
    """One one-cell symbol and foreground color."""

    symbol: str
    color: str

    def __post_init__(self) -> None:
        _validate_symbol(self.symbol, location="marker visual symbol")
        _validate_color(self.color, location="marker visual color")


@dataclass(frozen=True, slots=True)
class EventVisualStyle:
    """The inside/outside presentation for one marker evidence signature."""

    inside: MarkerVisual
    outside: MarkerVisual


@dataclass(frozen=True, slots=True)
class EventStyleRule:
    """One validated selector with partial visual overrides."""

    selector: str
    evidence_kinds: tuple[EvidenceKind, ...]
    specificity: tuple[int, int]
    symbol: str | None = None
    color: str | None = None
    outside_symbol: str | None = None
    outside_color: str | None = None

    def covers(self, mask: int) -> bool:
        """Return whether the selector covers every role retained by a marker."""

        return frozenset(evidence_kinds_from_mask(mask)).issubset(self.evidence_kinds)


@dataclass(frozen=True, slots=True)
class EventStyleRules:
    """Rules from one configuration precedence layer."""

    rules: tuple[EventStyleRule, ...] = ()


@dataclass(frozen=True, slots=True)
class EventStyleSheet:
    """Fully compiled visuals for every marker signature Workfold can produce."""

    visuals: tuple[tuple[int, EventVisualStyle], ...]

    def __post_init__(self) -> None:
        expected = supported_marker_evidence_masks()
        masks = tuple(mask for mask, _visual in self.visuals)
        if masks != expected:
            raise ValueError("an event style sheet must cover every supported marker signature exactly once")

    def style_for(self, mask: int) -> EventVisualStyle:
        for candidate, visual in self.visuals:
            if candidate == mask:
                return visual
        raise ValueError("no event style exists for the marker evidence signature")

    def visual_for(self, mask: int, *, within_schedule: bool) -> MarkerVisual:
        style = self.style_for(mask)
        return style.inside if within_schedule else style.outside


def parse_event_style_rules(value: object, *, location: str) -> EventStyleRules:
    """Validate one ``styles`` TOML table without applying precedence yet."""

    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a table keyed by event selectors")
    table = cast(dict[object, object], value)
    rules: list[EventStyleRule] = []
    normalized_selectors: set[str] = set()
    for raw_selector, raw_definition in table.items():
        if not isinstance(raw_selector, str):
            raise ValueError(f"{location} selector keys must be strings")
        selector = raw_selector.strip().lower()
        if selector in normalized_selectors:
            raise ValueError(f"{location} contains duplicate normalized selector {selector!r}")
        normalized_selectors.add(selector)
        try:
            selection = expand_evidence_selectors((selector,), option="style")
        except ValueError as error:
            raise ValueError(f"{location}.{raw_selector!r}: {error}") from error
        if not isinstance(raw_definition, dict):
            raise ValueError(f"{location}.{raw_selector!r} must be a table")
        definition = cast(dict[object, object], raw_definition)
        if any(not isinstance(key, str) for key in definition):
            raise ValueError(f"{location}.{raw_selector!r} property names must be strings")
        keys = cast(set[str], set(definition))
        unknown = sorted(keys - STYLE_KEYS)
        if unknown:
            raise ValueError(
                f"{location}.{raw_selector!r} has unknown property/properties: "
                + ", ".join(repr(key) for key in unknown)
            )
        if not definition:
            raise ValueError(f"{location}.{raw_selector!r} must override at least one style property")
        values = {key: _style_string(definition[key], location=f"{location}.{raw_selector!r}.{key}") for key in keys}
        for key in ("symbol", "outside-symbol"):
            if key in values:
                _validate_symbol(values[key], location=f"{location}.{raw_selector!r}.{key}")
        for key in ("color", "outside-color"):
            if key in values:
                _validate_color(values[key], location=f"{location}.{raw_selector!r}.{key}")
        parts = selector.split(":")
        rules.append(
            EventStyleRule(
                selector=selector,
                evidence_kinds=selection.kinds,
                specificity=(sum(part != "*" for part in parts), len(parts)),
                symbol=values.get("symbol"),
                color=values.get("color"),
                outside_symbol=values.get("outside-symbol"),
                outside_color=values.get("outside-color"),
            )
        )
    _validate_unambiguous_rules(tuple(rules), location=location)
    return EventStyleRules(tuple(rules))


def compile_event_style_sheet(layers: Sequence[EventStyleRules]) -> EventStyleSheet:
    """Apply global-to-local rules and wildcard specificity per marker signature."""

    visuals: list[tuple[int, EventVisualStyle]] = []
    for mask in supported_marker_evidence_masks():
        visual = _default_style(evidence_mask_source(mask))
        for layer in layers:
            applicable = tuple(
                rule for rule in sorted(layer.rules, key=lambda item: item.specificity) if rule.covers(mask)
            )
            for rule in applicable:
                visual = _apply_rule(visual, rule)
        visuals.append((mask, visual))
    return EventStyleSheet(tuple(visuals))


def _default_style(source: Source) -> EventVisualStyle:
    if source is Source.GIT:
        return EventVisualStyle(MarkerVisual("●", "green"), MarkerVisual("○", "bright_red"))
    return EventVisualStyle(MarkerVisual("■", "bright_blue"), MarkerVisual("□", "bright_red"))


def _apply_rule(visual: EventVisualStyle, rule: EventStyleRule) -> EventVisualStyle:
    return EventVisualStyle(
        inside=replace(
            visual.inside,
            symbol=rule.symbol if rule.symbol is not None else visual.inside.symbol,
            color=rule.color if rule.color is not None else visual.inside.color,
        ),
        outside=replace(
            visual.outside,
            symbol=rule.outside_symbol if rule.outside_symbol is not None else visual.outside.symbol,
            color=rule.outside_color if rule.outside_color is not None else visual.outside.color,
        ),
    )


def _style_string(value: object, *, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    if not value:
        raise ValueError(f"{location} must not be empty")
    return value


def _validate_symbol(value: str, *, location: str) -> None:
    spans, width = split_graphemes(value)
    if len(spans) != 1 or width != 1:
        raise ValueError(f"{location} must be exactly one printable terminal cell")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{location} must not contain terminal control characters")


def _validate_color(value: str, *, location: str) -> None:
    try:
        Color.parse(value)
    except ColorParseError as error:
        raise ValueError(f"{location} is not a valid terminal color: {value!r}") from error


def _validate_unambiguous_rules(rules: tuple[EventStyleRule, ...], *, location: str) -> None:
    attributes = ("symbol", "color", "outside_symbol", "outside_color")
    for mask in supported_marker_evidence_masks():
        candidates = tuple(rule for rule in rules if rule.covers(mask))
        for specificity in {rule.specificity for rule in candidates}:
            peers = tuple(rule for rule in candidates if rule.specificity == specificity)
            for attribute in attributes:
                configured = tuple(
                    (rule.selector, cast(str, value))
                    for rule in peers
                    if (value := getattr(rule, attribute)) is not None
                )
                if len({value for _selector, value in configured}) <= 1:
                    continue
                signature = " + ".join(kind.value for kind in evidence_kinds_from_mask(mask))
                selectors = ", ".join(repr(selector) for selector, _value in configured)
                property_name = attribute.replace("_", "-")
                raise ValueError(
                    f"{location} has ambiguous equally specific rules {selectors} for "
                    f"{signature} property {property_name!r}"
                )


DEFAULT_EVENT_STYLE_SHEET = compile_event_style_sheet(())

__all__ = [
    "DEFAULT_EVENT_STYLE_SHEET",
    "EventStyleRule",
    "EventStyleRules",
    "EventStyleSheet",
    "EventVisualStyle",
    "MarkerVisual",
    "compile_event_style_sheet",
    "parse_event_style_rules",
]
