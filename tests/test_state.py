from __future__ import annotations

from server.state import (
    MAX_HISTORY_MESSAGES,
    CharacterSheet,
    Session,
    WorldState,
    ability_modifier,
    proficiency_bonus_for_level,
)


def make_character(**overrides) -> CharacterSheet:
    defaults = dict(player_id="p1", name="Rook", hp=8, max_hp=10)
    defaults.update(overrides)
    return CharacterSheet(**defaults)


def test_ac_defaults_to_the_unarmored_baseline():
    # A real, stored field (unlike stat_modifiers), so this default matters
    # for any sheet server/engine.py's build_starting_character doesn't
    # explicitly compute a real value for - a blank/unrecognized class, a
    # legacy/pre-this-feature imported character, or a bare test fixture.
    character = make_character()
    assert character.ac == 10


def test_ability_modifier_standard_5e_formula():
    # A handful of real reference points from the SRD's own ability
    # modifier table - even scores are exact, odd scores round down.
    assert ability_modifier(10) == 0
    assert ability_modifier(11) == 0
    assert ability_modifier(15) == 2
    assert ability_modifier(8) == -1
    assert ability_modifier(20) == 5
    assert ability_modifier(1) == -5


def test_proficiency_bonus_for_level_standard_5e_table():
    # Real reference points from the SRD's own Proficiency Bonus by Level
    # table - +2 through level 4, then +1 every 4 levels.
    assert proficiency_bonus_for_level(1) == 2
    assert proficiency_bonus_for_level(4) == 2
    assert proficiency_bonus_for_level(5) == 3
    assert proficiency_bonus_for_level(8) == 3
    assert proficiency_bonus_for_level(9) == 4
    assert proficiency_bonus_for_level(13) == 5
    assert proficiency_bonus_for_level(17) == 6
    assert proficiency_bonus_for_level(20) == 6


def test_proficiency_bonus_computed_field_tracks_level():
    character = make_character(level=1)
    assert character.proficiency_bonus == 2
    character.level = 5
    assert character.proficiency_bonus == 3


def test_proficiency_bonus_included_in_model_dump():
    character = make_character(level=9)
    assert character.model_dump()["proficiency_bonus"] == 4


def test_spell_save_dc_for_a_real_caster():
    # wizard, level 1 (+2 proficiency), INT 16 (+3 modifier): 8 + 2 + 3 = 13.
    character = make_character(character_class="wizard", stats={"int": 16}, level=1)
    assert character.spell_save_dc == 13


def test_spell_save_dc_scales_with_proficiency_bonus():
    character = make_character(character_class="cleric", stats={"wis": 14}, level=9)
    assert character.spell_save_dc == 8 + 4 + 2  # level 9 -> +4 proficiency, WIS 14 -> +2


def test_spell_save_dc_is_none_for_a_non_caster():
    character = make_character(character_class="fighter", stats={"str": 15})
    assert character.spell_save_dc is None


def test_spell_save_dc_is_none_for_a_blank_class():
    character = make_character()
    assert character.spell_save_dc is None


def test_known_spells_and_spell_slots_default_to_empty():
    character = make_character()
    assert character.known_spells == []
    assert character.spell_slots == {}
    assert character.max_spell_slots == {}


def test_stat_modifiers_computed_field_is_empty_when_stats_is_empty():
    character = make_character()
    assert character.stats == {}
    assert character.stat_modifiers == {}


def test_stat_modifiers_computed_field_reflects_real_stats():
    character = make_character(stats={"str": 15, "dex": 8, "con": 10})
    assert character.stat_modifiers == {"str": 2, "dex": -1, "con": 0}


def test_stat_modifiers_included_in_model_dump_and_json():
    character = make_character(stats={"str": 15})
    assert character.model_dump()["stat_modifiers"] == {"str": 2}
    assert '"stat_modifiers":{"str":2}' in character.model_dump_json()


def test_constructing_with_a_stat_modifiers_kwarg_is_silently_ignored():
    # A previously-exported character file (client/app.py's
    # export_character) carries stat_modifiers in its JSON, since it's
    # just self.my_character - a real CharacterSheet.model_dump(). Passing
    # it back into the constructor (server/engine.py's
    # _character_from_import) must not error just because it's a
    # read-only computed field, not a real settable one - pydantic's
    # default extra="ignore" behavior already covers this, this is a
    # regression lock on that specific real-world shape.
    character = CharacterSheet(
        player_id="p1", name="Rook", hp=10, max_hp=10, stats={"str": 15}, stat_modifiers={"str": 999}
    )
    assert character.stat_modifiers == {"str": 2}  # recomputed from the real stats, not the bogus input


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


def test_apply_update_long_rest_fully_restores_hp():
    character = make_character(hp=3, max_hp=10)
    result = character.apply_update({"rest": "long"})
    assert character.hp == 10
    assert "long rest" in result and "10/10" in result


def test_apply_update_long_rest_at_full_hp_is_a_no_op():
    character = make_character(hp=10, max_hp=10)
    result = character.apply_update({"rest": "long"})
    assert character.hp == 10
    assert result.startswith("No changes applied")


def test_apply_update_short_rest_restores_half_of_missing_hp():
    character = make_character(hp=2, max_hp=10)  # missing 8, floor(8/2) = 4
    result = character.apply_update({"rest": "short"})
    assert character.hp == 6
    assert "short rest" in result and "+4" in result and "6/10" in result


def test_apply_update_short_rest_floors_an_odd_missing_amount():
    character = make_character(hp=7, max_hp=10)  # missing 3, floor(3/2) = 1
    character.apply_update({"rest": "short"})
    assert character.hp == 8


def test_apply_update_short_rest_at_full_hp_is_a_no_op():
    character = make_character(hp=10, max_hp=10)
    result = character.apply_update({"rest": "short"})
    assert character.hp == 10
    assert result.startswith("No changes applied")


def test_apply_update_short_rest_never_overshoots_max_hp():
    # missing=1, floor(1/2)=0 - a genuinely near-full character shouldn't
    # somehow tip over max_hp from a rounding quirk.
    character = make_character(hp=9, max_hp=10)
    character.apply_update({"rest": "short"})
    assert character.hp == 9


def test_apply_update_rest_leaves_conditions_untouched():
    # Deliberate: most SRD conditions don't just expire with time under
    # the actual rules, so a rest silently clearing them would be a real
    # rules error, not a simplification - see the comment in state.py.
    character = make_character(hp=1, max_hp=10, conditions=["poisoned"])
    character.apply_update({"rest": "long"})
    assert character.conditions == ["poisoned"]


def test_apply_update_hp_drop_to_zero_begins_dying():
    character = make_character(hp=3, max_hp=10)
    result = character.apply_update({"hp_delta": -3})
    assert character.hp == 0
    assert character.dying is True
    assert character.death_save_successes == 0
    assert character.death_save_failures == 0
    assert "dying" in result


def test_apply_update_damage_while_already_down_is_an_automatic_failure():
    character = make_character(hp=0, max_hp=10, dying=True)
    result = character.apply_update({"hp_delta": -4})
    assert character.hp == 0  # clamped, already at the floor
    assert character.death_save_failures == 1
    assert character.dying is True  # one failure alone doesn't end it
    assert "failure" in result


def test_apply_update_three_automatic_failures_while_down_kills():
    character = make_character(hp=0, max_hp=10, dying=True, death_save_failures=2)
    character.apply_update({"hp_delta": -2})
    assert character.dead is True
    assert character.dying is False


def test_apply_update_healing_above_zero_clears_dying():
    character = make_character(hp=0, max_hp=10, dying=True, death_save_successes=1, death_save_failures=1)
    result = character.apply_update({"hp_delta": 5})
    assert character.hp == 5
    assert character.dying is False
    assert character.death_save_successes == 0
    assert character.death_save_failures == 0
    assert "stabilizes" in result


def test_apply_update_long_rest_also_clears_dying():
    # Healing via rest, not just a positive hp_delta - both paths mutate
    # self.hp, so both need to hit the same "healed above 0" check.
    character = make_character(hp=0, max_hp=10, dying=True)
    character.apply_update({"rest": "long"})
    assert character.hp == 10
    assert character.dying is False


def test_apply_update_never_re_enters_dying_once_dead():
    character = make_character(hp=0, max_hp=10, dead=True)
    result = character.apply_update({"hp_delta": -1})
    assert character.dying is False  # dead is a final state, not a re-triggerable one
    assert "dying" not in result


def test_record_death_save_three_successes_stabilizes():
    character = make_character(hp=0, max_hp=10, dying=True, death_save_successes=2)
    result = character.record_death_save(success=True)
    assert character.dying is False
    assert character.dead is False
    assert character.death_save_successes == 0
    assert "stabilizes" in result


def test_record_death_save_three_failures_kills():
    character = make_character(hp=0, max_hp=10, dying=True, death_save_failures=2)
    result = character.record_death_save(success=False)
    assert character.dead is True
    assert character.dying is False
    assert "died" in result


def test_record_death_save_a_natural_one_counts_as_two_failures():
    character = make_character(hp=0, max_hp=10, dying=True)
    character.record_death_save(success=False, count=2)
    assert character.death_save_failures == 2
    assert character.dying is True


def test_record_death_save_a_natural_one_that_would_be_the_third_failure_stops_at_death():
    # count=2 on a nat 1 shouldn't record a phantom fourth failure once the
    # third already ended it - failures stays at exactly 3, not 4 (death,
    # unlike stabilize, doesn't reset the counters - they're left as the
    # real record of what killed the character).
    character = make_character(hp=0, max_hp=10, dying=True, death_save_failures=2)
    character.record_death_save(success=False, count=2)
    assert character.death_save_failures == 3
    assert character.dead is True


def test_apply_update_inventory_add_and_remove():
    character = make_character()

    character.apply_update({"add_item": "rusty key"})
    assert character.find_item("rusty key") is not None

    character.apply_update({"remove_item": "rusty key"})
    assert character.find_item("rusty key") is None


    result = character.apply_update({"remove_item": "nonexistent"})
    assert character.find_item("nonexistent") is None
    assert result.startswith("No changes applied")


def test_apply_update_heals_a_dead_character_back_to_life():
    character = make_character(hp=0, max_hp=10, dead=True)

    result = character.apply_update({"hp_delta": 5})

    assert character.dead is False
    assert character.dying is False
    assert character.death_save_failures == 0
    assert character.hp == 5
    assert "brought back from death" in result


def test_apply_update_without_healing_leaves_a_dead_character_dead():
    character = make_character(hp=0, max_hp=10, dead=True)

    result = character.apply_update({"notes": "still dead"})

    assert character.dead is True
    assert "brought back" not in result


def test_apply_update_add_item_with_magic_bonus():
    # magic_bonus only ever comes from the DM's own tool call - real 5e's
    # magic weapons/armor/shields, tracked on the item itself rather than
    # trusted to the model's own arithmetic at roll time.
    character = make_character()

    result = character.apply_update({"add_item": "Longsword", "magic_bonus": 1})

    item = character.find_item("Longsword")
    assert item is not None
    assert item.magic_bonus == 1
    assert "+1 Longsword" in result


def test_apply_update_add_item_stacks_identical_items():
    character = make_character()

    character.apply_update({"add_item": "Potion of Healing"})
    character.apply_update({"add_item": "Potion of Healing"})

    assert len(character.inventory) == 1
    assert character.find_item("Potion of Healing").quantity == 2


def test_apply_update_add_item_keeps_different_magic_bonus_as_a_separate_stack():
    character = make_character()

    character.apply_update({"add_item": "Longsword"})
    character.apply_update({"add_item": "Longsword", "magic_bonus": 1})

    assert len(character.inventory) == 2
    assert character.find_item("Longsword").magic_bonus == 0  # first match is still the mundane one


def test_apply_update_remove_item_decrements_a_stack_before_dropping_it():
    character = make_character()
    character.apply_update({"add_item": "Potion of Healing"})
    character.apply_update({"add_item": "Potion of Healing"})

    character.apply_update({"remove_item": "Potion of Healing"})
    assert character.find_item("Potion of Healing").quantity == 1

    character.apply_update({"remove_item": "Potion of Healing"})
    assert character.find_item("Potion of Healing") is None


def test_find_item_is_case_insensitive():
    character = make_character()
    character.apply_update({"add_item": "Rusty Key"})

    assert character.find_item("rusty key") is not None
    assert character.find_item("RUSTY KEY") is not None


def test_find_item_returns_none_for_blank_name():
    character = make_character()
    assert character.find_item(None) is None
    assert character.find_item("") is None


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


def test_apply_update_disposition():
    character = make_character()
    assert character.disposition == "neutral"

    result = character.apply_update({"disposition": "hostile"})
    assert character.disposition == "hostile"
    assert "disposition now hostile" in result

    # setting the exact same disposition again is a no-op
    result = character.apply_update({"disposition": "hostile"})
    assert result.startswith("No changes applied")


def test_apply_update_rejects_an_unrecognized_disposition():
    # A real model-input boundary: the field is a closed Literal, but the
    # update dict comes straight from a tool call with no guaranteed
    # server-side schema enforcement (OllamaNarrator's shared
    # update_character path in particular) - an unrecognized value must be
    # silently ignored, not written past the declared enum.
    character = make_character()

    result = character.apply_update({"disposition": "curious"})
    assert character.disposition == "neutral"
    assert result.startswith("No changes applied")


def test_world_apply_update_location_and_summary():
    world = WorldState()

    result = world.apply_update({"location": "The Rusty Anchor tavern", "summary": "A storm is coming."})
    assert world.location == "The Rusty Anchor tavern"
    assert world.summary == "A storm is coming."
    assert "location now" in result and "summary updated" in result


def test_world_apply_update_mood():
    world = WorldState()

    result = world.apply_update({"mood": "tense"})
    assert world.mood == "tense"
    assert "mood now 'tense'" in result

    result = world.apply_update({"mood": "tense"})
    assert result.startswith("No changes applied")

    result = world.apply_update({"mood": "foreboding"})
    assert world.mood == "foreboding"
    assert "mood now 'foreboding'" in result


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


def test_world_apply_update_objective_expires():
    world = WorldState()
    world.apply_update({"add_objective": "Meet the caravan before dawn"})

    result = world.apply_update({"expire_objective": "Meet the caravan before dawn"})
    assert world.objectives[0].status == "expired"
    assert "expired" in result

    # already-expired shouldn't flip to failed or re-expire
    result = world.apply_update({"fail_objective": "Meet the caravan before dawn"})
    assert world.objectives[0].status == "expired"
    assert result.startswith("No changes applied")


def test_world_apply_update_objective_fails():
    world = WorldState()
    world.apply_update({"add_objective": "Capture the bandit leader alive"})

    result = world.apply_update({"fail_objective": "Capture the bandit leader alive"})
    assert world.objectives[0].status == "failed"
    assert "failed" in result

    # already-failed shouldn't flip to completed
    result = world.apply_update({"complete_objective": "Capture the bandit leader alive"})
    assert world.objectives[0].status == "failed"


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


def test_world_apply_update_add_location():
    world = WorldState()

    result = world.apply_update({"add_location": "Great Hall"})
    assert world.location_map == {"Great Hall": []}
    assert "added location" in result

    # adding an already-registered location again is a no-op
    result = world.apply_update({"add_location": "Great Hall"})
    assert result.startswith("No changes applied")


def test_world_apply_update_connect_locations_is_two_way_and_adds_new_endpoints():
    world = WorldState()

    result = world.apply_update({"connect_locations": ["Great Hall", "Armory"]})
    assert world.location_map["Great Hall"] == ["Armory"]
    assert world.location_map["Armory"] == ["Great Hall"]
    assert "connected" in result


def test_world_apply_update_connect_locations_repeat_call_is_a_no_op():
    world = WorldState()
    world.apply_update({"connect_locations": ["Great Hall", "Armory"]})

    result = world.apply_update({"connect_locations": ["Great Hall", "Armory"]})
    assert result.startswith("No changes applied")
    assert world.location_map["Great Hall"] == ["Armory"]  # not duplicated


def test_world_apply_update_connect_locations_ignores_a_self_connection():
    # a == b would otherwise append the same name to the same list twice
    # (location_map[a] and location_map[b] are the same list object) -
    # this locks the guard that prevents that corruption.
    world = WorldState()

    result = world.apply_update({"connect_locations": ["Great Hall", "Great Hall"]})
    assert result.startswith("No changes applied")
    assert world.location_map == {}


def test_world_apply_update_no_matching_keys_reports_no_change():
    world = WorldState()
    result = world.apply_update({})
    assert result.startswith("No changes applied")


def test_narrator_context_is_empty_for_a_fresh_world():
    world = WorldState()
    assert world.narrator_context() == ""


def test_narrator_context_includes_location_when_known():
    world = WorldState()
    world.apply_update({"location": "Millbrook"})
    assert world.narrator_context() == "Current location: Millbrook"


def test_narrator_context_includes_mood_when_set():
    world = WorldState(location="Millbrook", mood="tense")

    assert world.narrator_context() == "Current location: Millbrook\nCurrent mood: tense"


def test_narrator_context_includes_summary_when_set():
    # summary was previously only ever surfaced to a reconnecting player via
    # _resume_recap() (server/engine.py) - never to the DM's own per-turn
    # context, so it could scroll out of Session.history's rolling window
    # on an ordinary long session exactly as easily as a reconnect gap
    # (ROADMAP.md item 1). Same field now doing double duty.
    world = WorldState(location="Millbrook", summary="The party freed the village from bandits.")

    assert world.narrator_context() == (
        "Current location: Millbrook\nThe party freed the village from bandits."
    )


def test_narrator_context_orders_summary_between_location_and_mood():
    world = WorldState(
        location="Millbrook", summary="The party freed the village from bandits.", mood="hopeful"
    )

    assert world.narrator_context() == (
        "Current location: Millbrook\nThe party freed the village from bandits.\nCurrent mood: hopeful"
    )


def test_narrator_context_includes_only_active_objectives():
    # Given to the DM specifically so complete_objective can copy an
    # exact text match (ROADMAP.md's update_world reliability
    # investigation) - an already-completed/expired/failed objective has
    # nothing left to complete, so listing it here would be noise, not
    # help.
    world = WorldState()
    world.apply_update({"add_objective": "Find the missing goat"})
    world.apply_update({"add_objective": "Deliver the letter"})
    world.apply_update({"complete_objective": "Deliver the letter"})

    context = world.narrator_context()
    assert "Active objectives:" in context
    assert "- Find the missing goat" in context
    assert "Deliver the letter" not in context


def test_narrator_context_combines_location_and_objectives():
    world = WorldState()
    world.apply_update({"location": "Millbrook", "add_objective": "Find the missing goat"})

    context = world.narrator_context()
    assert context == "Current location: Millbrook\nActive objectives:\n- Find the missing goat"


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
