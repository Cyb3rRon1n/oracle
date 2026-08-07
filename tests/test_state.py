from __future__ import annotations

from server.state import MAX_HISTORY_MESSAGES, CharacterSheet, Session


def make_character(**overrides) -> CharacterSheet:
    defaults = dict(player_id="p1", name="Rook", hp=8, max_hp=10)
    defaults.update(overrides)
    return CharacterSheet(**defaults)


def test_apply_update_hp_delta_damage_and_healing():
    character = make_character(hp=8, max_hp=10)

    result = character.apply_update({"hp_delta": -5})
    assert character.hp == 3
    assert "HP -5" in result and "now 3/10" in result

    character.apply_update({"hp_delta": 2})
    assert character.hp == 5


def test_apply_update_clamps_hp_to_valid_range():
    character = make_character(hp=2, max_hp=10)
    character.apply_update({"hp_delta": -100})
    assert character.hp == 0

    character = make_character(hp=9, max_hp=10)
    character.apply_update({"hp_delta": 100})
    assert character.hp == 10


def test_apply_update_inventory_add_and_remove():
    character = make_character()

    character.apply_update({"add_item": "rusty key"})
    assert "rusty key" in character.inventory

    character.apply_update({"remove_item": "rusty key"})
    assert "rusty key" not in character.inventory

    # removing something not present is a no-op, not an error
    result = character.apply_update({"remove_item": "nonexistent"})
    assert "nonexistent" not in character.inventory
    assert result.startswith("No changes applied")


def test_apply_update_conditions_add_and_remove():
    character = make_character()

    character.apply_update({"add_condition": "poisoned"})
    assert "poisoned" in character.conditions

    character.apply_update({"add_condition": "poisoned"})  # idempotent
    assert character.conditions.count("poisoned") == 1

    character.apply_update({"remove_condition": "poisoned"})
    assert "poisoned" not in character.conditions


def test_apply_update_empty_or_zero_delta_reports_no_change():
    character = make_character(hp=8)
    result = character.apply_update({"hp_delta": 0})
    assert character.hp == 8
    assert result.startswith("No changes applied")


def test_append_turn_accumulates_history():
    session = Session(session_id="s")
    session.append_turn("hello", "hi there")

    assert session.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_append_turn_caps_rolling_window():
    session = Session(session_id="s")
    for i in range(10):
        session.append_turn(f"action {i}", f"narration {i}")

    assert len(session.history) == MAX_HISTORY_MESSAGES
    assert session.history[-2:] == [
        {"role": "user", "content": "action 9"},
        {"role": "assistant", "content": "narration 9"},
    ]
    # 10 exchanges (20 messages) capped to the last 6 (12 messages) drops
    # actions 0-3, so the oldest surviving turn is action 4.
    assert session.history[0] == {"role": "user", "content": "action 4"}


def test_append_turn_respects_custom_max_history_messages():
    session = Session(session_id="s", max_history_messages=4)
    for i in range(5):
        session.append_turn(f"action {i}", f"narration {i}")

    assert len(session.history) == 4
    assert session.history[0] == {"role": "user", "content": "action 3"}


def test_append_turn_with_zero_max_history_keeps_no_history():
    # A naive `history[-0:]` slice is the *whole* list in Python (no negative
    # zero for ints) - without an explicit guard, max_history_messages=0
    # would silently keep everything instead of nothing.
    session = Session(session_id="s", max_history_messages=0)
    session.append_turn("action 0", "narration 0")

    assert session.history == []
