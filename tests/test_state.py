from __future__ import annotations

from server.state import MAX_HISTORY_MESSAGES, CharacterSheet, Session, WorldState


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


XP_THRESHOLDS = {1: 0, 2: 300, 3: 900, 4: 2700, 5: 6500}


def test_gain_xp_below_threshold_does_not_level_up():
    character = make_character()
    levels_gained = character.gain_xp(200, XP_THRESHOLDS)

    assert character.xp == 200
    assert character.level == 1
    assert levels_gained == 0


def test_gain_xp_crossing_one_threshold_levels_up_once():
    character = make_character()
    levels_gained = character.gain_xp(300, XP_THRESHOLDS)

    assert character.xp == 300
    assert character.level == 2
    assert levels_gained == 1


def test_gain_xp_crossing_multiple_thresholds_at_once_levels_up_repeatedly():
    # A single large award (a tough boss, or several stacked kills in one
    # turn) can plausibly jump more than one level at once - gain_xp loops
    # rather than checking the next threshold only once.
    character = make_character()
    levels_gained = character.gain_xp(2700, XP_THRESHOLDS)

    assert character.xp == 2700
    assert character.level == 4
    assert levels_gained == 3


def test_gain_xp_caps_at_the_highest_known_level():
    character = make_character(level=5, xp=6500)
    levels_gained = character.gain_xp(1_000_000, XP_THRESHOLDS)

    assert character.level == 5  # XP_THRESHOLDS tops out at level 5
    assert levels_gained == 0
    assert character.xp == 1_006_500  # xp itself still accumulates


def test_gain_xp_zero_or_negative_amount_is_a_no_op():
    character = make_character()
    assert character.gain_xp(0, XP_THRESHOLDS) == 0
    assert character.xp == 0
    assert character.gain_xp(-10, XP_THRESHOLDS) == 0
    assert character.xp == 0


def test_apply_update_notes():
    character = make_character()

    result = character.apply_update({"notes": "Wary of strangers, owes the party a favor."})
    assert character.notes == "Wary of strangers, owes the party a favor."
    assert "notes updated" in result

    # setting the exact same note again is a no-op
    result = character.apply_update({"notes": "Wary of strangers, owes the party a favor."})
    assert result.startswith("No changes applied")


def test_world_apply_update_location_and_summary():
    world = WorldState()

    result = world.apply_update({"location": "The Rusty Anchor tavern", "summary": "A storm is coming."})
    assert world.location == "The Rusty Anchor tavern"
    assert world.summary == "A storm is coming."
    assert "location now" in result and "summary updated" in result


def test_world_apply_update_objectives_add_complete_remove():
    world = WorldState()

    result = world.apply_update({"add_objective": "Find the missing merchant"})
    assert result.startswith("Applied")
    assert [o.text for o in world.objectives] == ["Find the missing merchant"]
    assert world.objectives[0].status == "active"

    # adding the exact same objective text again is a no-op
    result = world.apply_update({"add_objective": "Find the missing merchant"})
    assert result.startswith("No changes applied")

    result = world.apply_update({"complete_objective": "Find the missing merchant"})
    assert world.objectives[0].status == "completed"
    assert "completed" in result

    world.apply_update({"add_objective": "Escort the caravan"})
    result = world.apply_update({"remove_objective": "Escort the caravan"})
    assert [o.text for o in world.objectives] == ["Find the missing merchant"]
    assert "removed objective" in result


def test_world_apply_update_flags_set_and_clear():
    world = WorldState()

    result = world.apply_update({"set_flag": "met_the_baron"})
    assert world.flags["met_the_baron"] is True
    assert "flag set" in result

    # setting an already-true flag again is a no-op
    result = world.apply_update({"set_flag": "met_the_baron"})
    assert result.startswith("No changes applied")

    result = world.apply_update({"clear_flag": "met_the_baron"})
    assert world.flags["met_the_baron"] is False
    assert "flag cleared" in result


def test_world_apply_update_no_matching_keys_reports_no_change():
    world = WorldState()
    result = world.apply_update({})
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
