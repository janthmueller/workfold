from __future__ import annotations

import pytest
from workfold.configuration.styles import (
    DEFAULT_EVENT_STYLE_SHEET,
    EventStyleSheet,
    MarkerVisual,
    compile_event_style_sheet,
    parse_event_style_rules,
)
from workfold.domain.evidence import EvidenceKind, evidence_mask


def _mask(*kinds: EvidenceKind) -> int:
    return evidence_mask(kinds)


def test_built_in_styles_preserve_current_source_and_schedule_visuals() -> None:
    git = DEFAULT_EVENT_STYLE_SHEET.style_for(_mask(EvidenceKind.GIT_TAG_TAGGER))
    filesystem = DEFAULT_EVENT_STYLE_SHEET.style_for(_mask(EvidenceKind.FS_DIRECTORY_ACCESSED))

    assert (git.inside.symbol, git.inside.color) == ("●", "green")
    assert (git.outside.symbol, git.outside.color) == ("○", "bright_red")
    assert (filesystem.inside.symbol, filesystem.inside.color) == ("■", "bright_blue")
    assert (filesystem.outside.symbol, filesystem.outside.color) == ("□", "bright_red")


def test_layers_merge_properties_and_more_specific_rules_win_within_a_layer() -> None:
    global_rules = parse_event_style_rules(
        {
            "git:commit:author": {"symbol": "A"},
            "git:*": {"symbol": "G", "color": "yellow"},
            "*": {"outside-color": "magenta"},
        },
        location="global styles",
    )
    local_rules = parse_event_style_rules(
        {"git:*": {"color": "cyan"}, "git:tag:*": {"symbol": "T"}},
        location="local styles",
    )

    sheet = compile_event_style_sheet((global_rules, local_rules))

    author = sheet.style_for(_mask(EvidenceKind.GIT_COMMIT_AUTHOR))
    tag = sheet.style_for(_mask(EvidenceKind.GIT_TAG_TAGGER))
    filesystem = sheet.style_for(_mask(EvidenceKind.FS_FILE_MODIFIED))
    assert (author.inside.symbol, author.inside.color) == ("A", "cyan")
    assert (tag.inside.symbol, tag.inside.color) == ("T", "cyan")
    assert filesystem.inside.symbol == "■"
    assert author.outside.color == tag.outside.color == filesystem.outside.color == "magenta"


def test_coalesced_roles_require_one_rule_covering_the_complete_marker() -> None:
    exact_only = parse_event_style_rules(
        {"git:commit:author": {"symbol": "A"}},
        location="styles",
    )
    wildcard = parse_event_style_rules(
        {"git:commit:*": {"symbol": "C"}},
        location="styles",
    )
    coalesced = _mask(EvidenceKind.GIT_COMMIT_AUTHOR, EvidenceKind.GIT_COMMIT_COMMITTER)

    assert compile_event_style_sheet((exact_only,)).style_for(coalesced).inside.symbol == "●"
    assert compile_event_style_sheet((exact_only, wildcard)).style_for(coalesced).inside.symbol == "C"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"git:*": {}}, "at least one"),
        ({"git:*": {"symbol": "XX"}}, "one printable terminal cell"),
        ({"git:*": {"symbol": "\n"}}, "one printable terminal cell"),
        ({"git:*": {"color": "not-a-color"}}, "valid terminal color"),
        ({"git:*": {"weight": "bold"}}, "unknown property"),
        ({"remote:*": {"symbol": "R"}}, "unknown style selector"),
        (
            {"git:*:author": {"color": "cyan"}, "git:commit:*": {"color": "yellow"}},
            "ambiguous equally specific rules",
        ),
    ],
)
def test_invalid_style_rules_are_actionable(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_event_style_rules(value, location="styles")


def test_programmatic_style_models_enforce_complete_one_cell_visuals() -> None:
    with pytest.raises(ValueError, match="one printable terminal cell"):
        MarkerVisual("XX", "green")
    with pytest.raises(ValueError, match="valid terminal color"):
        MarkerVisual("X", "not-a-color")
    with pytest.raises(ValueError, match="every supported marker signature"):
        EventStyleSheet(())
