from __future__ import annotations

# Identity glyph allocation belongs to terminal presentation, not the domain.
from wuf.domain.identity import MarkerIdentity, RecordedIdentity
from wuf.reporting.terminal.identity import allocate_identity_symbols


def _identity(name: str, email: str) -> MarkerIdentity:
    return MarkerIdentity((RecordedIdentity(name, email),))


def test_identity_symbol_collisions_are_deterministic_and_unique() -> None:
    identities = (
        _identity("Ada Person", "ada@example.test"),
        _identity("Alice Person", "alice@example.test"),
        _identity("Bob Person", "bob@example.test"),
    )

    first = allocate_identity_symbols(identities)
    second = allocate_identity_symbols(identities)

    assert first == second
    assert [item.code for item in first] == ["A1", "A2", "B"]
    assert len({item.code for item in first}) == len(first)


def test_identity_symbol_allocator_numbers_initial_groups_without_an_upper_bound() -> None:
    identities = tuple(_identity("Person", f"person{index}@example.test") for index in range(30))

    symbols = allocate_identity_symbols(identities)

    assert len({item.code for item in symbols}) == 30
    assert [item.code for item in symbols] == [f"P{index}" for index in range(1, 31)]
    assert symbols[-1].marker(within_schedule=False) == "p30"


def test_identity_symbol_initial_falls_back_from_normalized_name_to_email() -> None:
    identities = (
        _identity("Äda", "ada@example.test"),
        _identity("", "bob@example.test"),
        _identity("", ""),
    )

    symbols = allocate_identity_symbols(identities)

    assert [item.code for item in symbols] == ["A", "B", "I"]
    assert [item.marker(within_schedule=False) for item in symbols] == ["a", "b", "i"]


def test_composite_identity_symbols_preserve_no_color_schedule_status() -> None:
    first_composite = MarkerIdentity(
        (
            RecordedIdentity("Ada", "ada@example.test"),
            RecordedIdentity("Bob", "bob@example.test"),
        )
    )
    second_composite = MarkerIdentity(
        (
            RecordedIdentity("Carol", "carol@example.test"),
            RecordedIdentity("Dan", "dan@example.test"),
        )
    )

    first, second = allocate_identity_symbols((first_composite, second_composite))

    assert first.code == "◆"
    assert first.marker(within_schedule=True) == "◆"
    assert first.marker(within_schedule=False) == "◇"
    assert second.code == "◆2"
    assert second.marker(within_schedule=False) == "◇2"
