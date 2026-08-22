from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from server.engine import (
    GameEngine,
    build_starting_character,
    _apply_ability_score_improvements,
    _asi_announcement,
    _compute_ac,
    _outcome_category,
    _owner_character_view,
    _public_character_view,
)
from server.lore import Guardian, Region, WhoWhatWhereWhenWhy, WorldBible
from server.rules import RulesIndex
from server.state import Objective, Session
from shared.protocol import Envelope


def make_world_bible(**overrides) -> WorldBible:
    defaults = dict(
        setting_name="Testonia",
        tagline="A place for testing.",
        cosmology="It exists solely to be asserted against.",
        guardian=Guardian(name="Testwarden", title="the Fixture", persona="Reliable."),
        regions=[Region(name="The Only Region", description="There is only one.")],
        central_tension="Will the assertions pass?",
        who_what_where_when_why=WhoWhatWhereWhenWhy(
            who="A test subject.", what="A test event.", where="A test place.",
            when="Test time.", why="For coverage.",
        ),
        tone_guidance="Dry and deterministic.",
    )
    defaults.update(overrides)
    return WorldBible(**defaults)


class StubDM:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        self.calls.append(list(history))  # snapshot — engine mutates session.history in place after this call
        for word in ["You ", "swing ", "your ", "sword."]:
            yield word


class FailingDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator


class UpdateCharacterDM:
    """Narrates a fixed line and calls apply_update with the given tool input,
    simulating the DM invoking the update_character tool mid-turn."""

    def __init__(self, update: dict):
        self._update = update
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        self.tool_result = apply_update(self._update)
        yield "You feel the effects immediately."


class UpdateSequenceDM:
    """Calls apply_update once per dict in updates, in order, simulating
    several update_character tool calls within a single DM turn (or, across
    separate .handle() calls, across separate turns)."""

    def __init__(self, updates: list[dict]):
        self._updates = updates
        self.tool_results: list[str] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        for update in self._updates:
            self.tool_results.append(apply_update(update))
        yield "Something happens."


class RequestRollDM:
    """Calls request_roll with the given tool input, simulating the DM
    invoking request_roll mid-turn, then narrates."""

    def __init__(self, roll_input: dict):
        self._roll_input = roll_input
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        self.tool_result = request_roll(self._roll_input)
        yield "You attempt it."


class GrantsItemThenRollsWeaponDM:
    """Calls apply_update (e.g. granting a magic weapon) then request_roll
    in the same turn, simulating a DM narrating a found item and an
    attack with it together - the structured-items magic_bonus wiring
    needs both tools in one turn to exercise end-to-end."""

    def __init__(self, add_item_update: dict, roll_input: dict):
        self._add_item_update = add_item_update
        self._roll_input = roll_input
        self.roll_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        apply_update(self._add_item_update)
        self.roll_result = request_roll(self._roll_input)
        yield "You feel the new blade's power."


class UpdateWorldDM:
    """Calls update_world with the given tool input, simulating the DM
    invoking update_world mid-turn, then narrates."""

    def __init__(self, world_update: dict):
        self._world_update = world_update
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        self.tool_result = update_world(self._world_update)
        yield "The story moves on."


class OpeningSceneDM:
    """Records the action_text (and world_summary) of every narrate() call
    it receives, and always narrates the same fixed text - used to test
    the opening-scene hook without depending on real content."""

    def __init__(self):
        self.action_texts: list[str] = []
        self.world_summaries: list[str | None] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        self.action_texts.append(action_text)
        self.world_summaries.append(world_summary)
        yield "Scene."


def make_engine(dm, enable_opening_scene=False):
    # enable_opening_scene defaults off here so the many existing tests that
    # just call join() and don't care about the opening-scene feature keep
    # their original "join() has no narration side effect" semantics
    # unchanged. Tests for the feature itself opt in explicitly.
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env: Envelope):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env: Envelope):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, dm, broadcast, send_to, enable_opening_scene=enable_opening_scene)
    return engine, session, received


async def join(engine, player_id, name="Thrain"):
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": name},
    ))


async def test_join_seats_player_and_starts_their_turn():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    assert session.current_turn == player_id


@pytest.mark.parametrize(
    "character_class,expected_hp,expected_inventory,expected_stats,expected_ac",
    [
        # HP is the SRD hit_die max + a real CON modifier now (every class
        # places CON second in its own priority order - see
        # CLASS_ABILITY_PRIORITY - so each gets the Standard Array's 14,
        # a +2 modifier, uniformly): fighter d10+2=12, rogue d8+2=10,
        # cleric d8+2=10, wizard d6+2=8. AC is 11 (leather armor's base)
        # + DEX modifier for the three classes whose starting kit includes
        # it, or 10 (unarmored) + DEX modifier for wizard, which doesn't.
        ("fighter", 12, ["Longsword", "Leather Armor"],
         {"str": 15, "con": 14, "dex": 13, "wis": 12, "cha": 10, "int": 8}, 12),
        ("rogue", 10, ["Shortbow", "Leather Armor"],
         {"dex": 15, "con": 14, "int": 13, "wis": 12, "cha": 10, "str": 8}, 13),
        ("cleric", 10, ["Leather Armor", "Potion of Healing"],
         {"wis": 15, "con": 14, "str": 13, "dex": 12, "cha": 10, "int": 8}, 12),
        ("wizard", 8, ["Potion of Healing"],
         {"int": 15, "con": 14, "dex": 13, "wis": 12, "cha": 10, "str": 8}, 11),
    ],
)
def test_build_starting_character_gives_a_real_class_kit(
    character_class, expected_hp, expected_inventory, expected_stats, expected_ac
):
    # Closes the "no character sheet at all" gap: previously every fresh
    # character was just name + hp=10/10 with nothing else, since stats/
    # inventory otherwise only get populated if the DM's update_character
    # tool happens to fire mid-narration - unreliable per this project's
    # whole tool-call investigation. HP is the SRD hit_die's max value
    # plus a real CON modifier (ability scores closed that gap).
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", character_class, rules)

    assert sheet.hp == expected_hp
    assert sheet.max_hp == expected_hp
    assert [item.name for item in sheet.inventory] == expected_inventory
    assert all(item.quantity == 1 and item.magic_bonus == 0 for item in sheet.inventory)
    assert sheet.character_class  # the SRD's display name, e.g. "Fighter"
    assert sheet.stats == expected_stats
    assert sheet.stat_modifiers["con"] == 2  # (14 - 10) // 2
    assert sheet.ac == expected_ac


@pytest.mark.parametrize("character_class", ["", "bard", "not-a-real-class"])
def test_build_starting_character_falls_back_on_blank_or_unknown_class(character_class):
    # Old clients/tests that never send character_class at all, and a
    # typo'd/unsupported class, both keep working exactly like before
    # this feature existed - a blank sheet, not a crash.
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", character_class, rules)

    assert sheet.hp == 10
    assert sheet.max_hp == 10
    assert sheet.inventory == []
    assert sheet.character_class == ""
    assert sheet.stats == {}
    assert sheet.ac == 10  # unarmored baseline, no DEX modifier to add (no stats)


def test_build_starting_character_gives_wizard_real_known_spells_and_slots():
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Gandalf", "wizard", rules)

    assert sheet.known_spells == [
        "fire_bolt", "ray_of_frost", "magic_missile", "mage_armor", "shield", "fireball",
        "burning_hands", "misty_step", "sleep", "charm_person", "thunderwave",
        "hold_person", "web",
    ]
    assert sheet.spell_slots == {"1": 2}
    assert sheet.max_spell_slots == {"1": 2}
    assert sheet.spell_save_dc is not None


def test_build_starting_character_gives_cleric_real_known_spells_and_slots():
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Fenwick", "cleric", rules)

    assert sheet.known_spells == [
        "sacred_flame", "guidance", "cure_wounds", "bless", "healing_word", "spiritual_weapon",
        "inflict_wounds", "shield_of_faith", "guiding_bolt", "hold_person",
    ]
    assert sheet.spell_slots == {"1": 2}


def test_build_starting_character_gives_a_non_caster_no_spells():
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", "fighter", rules)

    assert sheet.known_spells == []
    assert sheet.spell_slots == {}
    assert sheet.max_spell_slots == {}


def test_expanded_monster_entries_resolve_to_real_xp_and_ac():
    # The 2026-08-21 SRD batch (hobgoblin/troll/...) - every entry must
    # resolve via the real lookup path, map its CR to a real XP award
    # (locks the cr strings against typos), and carry an AC that an
    # introduced NPC would copy.
    rules = RulesIndex.load_default()
    for name in ("hobgoblin", "gnoll", "ghoul", "harpy", "owlbear", "troll"):
        entry = rules.get_entry("monster", name)
        assert entry is not None, f"missing monster {name}"
        assert "ac" in entry, name
        assert rules.xp_for_cr(entry["cr"]), f"{name}: CR '{entry['cr']}' has no XP"


def test_third_srd_monster_batch_resolves_to_real_xp_and_ac():
    # The 2026-08-22 batch (giant_spider/bugbear/wight/basilisk) - same
    # real-lookup-path lock as the 2026-08-21 batch above.
    rules = RulesIndex.load_default()
    for name in ("giant_spider", "bugbear", "wight", "basilisk"):
        entry = rules.get_entry("monster", name)
        assert entry is not None, f"missing monster {name}"
        assert "ac" in entry, name
        assert rules.xp_for_cr(entry["cr"]), f"{name}: CR '{entry['cr']}' has no XP"


def test_build_starting_character_applies_race_ability_bonus_and_display_name():
    # Dwarf's real SRD ability_score_increase (+2 con) stacks additively on
    # top of fighter's own class-priority Standard Array assignment (con
    # already 14 there - see the class-kit test above) - 14 + 2 = 16, which
    # also raises the CON modifier feeding starting HP (10 + 3 instead of
    # the plain-fighter baseline's 10 + 2 = 12).
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", "fighter", rules, race="dwarf")

    assert sheet.race == "Dwarf"  # the SRD's display name, e.g. character_class
    assert sheet.stats["con"] == 16
    assert sheet.hp == 13
    assert sheet.max_hp == 13


def test_build_starting_character_applies_a_subrace_combined_ability_bonus():
    # hill_dwarf's own ability_score_increase ({"con": 2, "wis": 1}) is
    # already the combined base-race-plus-subrace total, not something
    # server/engine.py stacks itself - fighter's con is 14 before any
    # race bonus, wis is 12 (see the class-kit test above).
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", "fighter", rules, race="hill_dwarf")

    assert sheet.race == "Hill Dwarf"
    assert sheet.stats["con"] == 16  # 14 + 2
    assert sheet.stats["wis"] == 13  # 12 + 1


@pytest.mark.parametrize("race", ["", "gnome", "not-a-real-race"])
def test_build_starting_character_falls_back_on_blank_or_unknown_race(race):
    # Same graceful-miss convention build_starting_character's own class
    # handling already has - a blank/unrecognized race costs the ability
    # bonus and racial traits, not a crash, and doesn't touch stats at all.
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", "fighter", rules, race=race)

    assert sheet.race == ""
    assert sheet.stats["con"] == 14  # unchanged from the plain-fighter baseline


def test_build_starting_character_records_race_independent_of_class():
    # race and character_class are genuinely independent choices - a
    # classless character (blank/unrecognized class) still gets their race
    # recorded, even though there are no stats for a race bonus to apply to.
    rules = RulesIndex.load_default()
    sheet = build_starting_character("p1", "Rook", "", rules, race="elf")

    assert sheet.character_class == ""
    assert sheet.race == "Elf"
    assert sheet.stats == {}


def test_rules_index_spell_slots_by_level_real_progression():
    rules = RulesIndex.load_default()
    assert rules.spell_slots_by_level(1) == {"1": 2}
    assert rules.spell_slots_by_level(5) == {"1": 4, "2": 3, "3": 2}


def test_rules_index_spell_slots_by_level_unknown_level_is_empty():
    rules = RulesIndex.load_default()
    assert rules.spell_slots_by_level(999) == {}


async def test_join_with_character_class_builds_real_starting_sheet_end_to_end():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))

    character = session.characters[player_id]
    assert character.hp == 12  # d10 hit die max (10) + a real CON modifier (+2)
    assert character.character_class == "Fighter"
    assert [item.name for item in character.inventory] == ["Longsword", "Leather Armor"]


async def test_join_with_unrecognized_class_warns_the_player_privately():
    # A real, live-found gap (ROADMAP.md's campaign dry-run, 2026-08-10): a
    # typo'd/garbled class string used to silently produce a blank,
    # classless, stat-less character with no indication anything went
    # wrong - found live when a scripted class-field edit landed as
    # "clericrogue" instead of "cleric".
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "clericrogue"},
    ))

    character = session.characters[player_id]
    assert character.character_class == ""  # the existing graceful blank-fallback, unchanged
    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert any("clericrogue" in w[3]["text"] for w in warnings)


async def test_join_with_blank_class_is_not_treated_as_a_mistake():
    # Blank is the UI's own explicit "blank to skip" option (WelcomeScreen's
    # own Static label), not a typo - shouldn't get the unrecognized-class
    # warning above.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings == []


async def test_join_with_stat_priority_overrides_the_class_default():
    # Fighter's own CLASS_ABILITY_PRIORITY defaults to str-first
    # (see server/engine.py) - an explicit override should win instead,
    # the "broader stats survey" the original brainstorm asked for beyond
    # just a recommended class.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={
            "player_name": "Rook", "character_class": "fighter",
            "stat_priority": ["cha", "con", "dex", "wis", "int", "str"],
        },
    ))

    character = session.characters[player_id]
    assert character.stats == {"cha": 15, "con": 14, "dex": 13, "wis": 12, "int": 10, "str": 8}


async def test_join_with_invalid_stat_priority_falls_back_to_class_default():
    # Missing "cha"/duplicate "con" - not a real permutation of the 6
    # ability keys. The same graceful-miss convention every other name-
    # based field in this file already follows, rather than a
    # ValidationError on a malformed/adversarial payload.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={
            "player_name": "Rook", "character_class": "fighter",
            "stat_priority": ["con", "con", "dex", "wis", "int", "str"],
        },
    ))

    character = session.characters[player_id]
    assert character.stats == {"str": 15, "con": 14, "dex": 13, "wis": 12, "cha": 10, "int": 8}


async def test_join_with_race_builds_a_real_racial_bonus_end_to_end():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter", "race": "dwarf"},
    ))

    character = session.characters[player_id]
    assert character.race == "Dwarf"
    assert character.stats["con"] == 16
    assert character.hp == 13


async def test_join_with_unrecognized_race_warns_the_player_privately():
    # Same silent-mistake gap the unrecognized-class warning above closes,
    # for the same reason - a typo'd race string otherwise costs a player
    # their ability bonus and racial traits with no indication anything
    # went wrong.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter", "race": "gnomeling"},
    ))

    character = session.characters[player_id]
    assert character.race == ""  # the existing graceful blank-fallback, unchanged
    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert any("gnomeling" in w[3]["text"] for w in warnings)


async def test_join_with_blank_race_is_not_treated_as_a_mistake():
    # Blank is the UI's own explicit "blank to skip" option (WelcomeScreen's
    # own Static label), not a typo - shouldn't get the unrecognized-race
    # warning above.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings == []


async def test_join_generates_a_random_origin_regardless_of_class():
    # The near-death/transport premise (server/lore) applies to every new
    # character, not just ones who picked a recognized class - covers
    # both build_starting_character's real-class path and its blank/
    # unrecognized-class fallback.
    engine, session, _ = make_engine(StubDM())
    with_class_id, blank_class_id = str(uuid.uuid4()), str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=with_class_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=blank_class_id,
        payload={"player_name": "Nameless"},
    ))

    assert session.characters[with_class_id].background != ""
    assert session.characters[blank_class_id].background != ""


async def test_join_with_imported_character_uses_imported_sheet():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    imported = {
        "player_id": "some-other-stale-id",  # must never win over the real connection's id
        "name": "Torvin Ironheart",
        "hp": 7,
        "max_hp": 12,
        "character_class": "Cleric",
        "stats": {"str": 14},
        "inventory": [{"name": "Mace"}, {"name": "Holy Symbol", "quantity": 2}],
        "conditions": ["blessed"],
        "notes": "Sworn to protect the village of Rivenwood.",
        "xp": 450,
        "level": 3,
    }

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "ignored", "character_class": "ignored", "imported_character": imported},
    ))

    character = session.characters[player_id]
    assert character.player_id == player_id, "player_id is always the real connection's, never trusted from the file"
    assert character.name == "Torvin Ironheart"
    assert character.hp == 7
    assert character.max_hp == 12
    assert character.character_class == "Cleric"
    assert [item.name for item in character.inventory] == ["Mace", "Holy Symbol"]
    assert character.inventory[1].quantity == 2
    assert character.notes == "Sworn to protect the village of Rivenwood."
    assert character.xp == 450
    assert character.level == 3


async def test_join_with_legacy_plain_string_inventory_import_fails_gracefully():
    # An export file from before the structured-items feature had
    # inventory as a flat list of name strings, not {name, quantity,
    # magic_bonus} dicts - a real shape mismatch CharacterSheet(**imported)
    # now rejects. Falls back to a fresh character with a warning, the
    # same "any shape mismatch means no import, not a crash" behavior
    # _character_from_import already has for a corrupted/future-version
    # file - not a new special case, just this project's existing
    # fallback now also covering a real, previously-untested shape.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    imported = {
        "player_id": "stale", "name": "Old Export", "hp": 5, "max_hp": 10,
        "inventory": ["Mace", "Holy Symbol"],
    }

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "", "imported_character": imported},
    ))

    character = session.characters[player_id]
    assert character.name == "Rook"  # the fresh fallback, not the imported "Old Export"
    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert any("couldn't import" in w[3]["text"].lower() for w in warnings)


async def test_join_with_invalid_imported_character_falls_back_to_fresh_start():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={
            "player_name": "Rook", "character_class": "fighter",
            "imported_character": {"hp": "not-a-number"},  # wrong type, no required fields
        },
    ))

    character = session.characters[player_id]
    assert character.name == "Rook"
    assert character.character_class == "Fighter"  # fell back to build_starting_character

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert any("import" in w[3]["text"].lower() for w in warnings)


async def test_join_with_non_dict_imported_character_falls_back_to_fresh_start():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "imported_character": ["not", "a", "dict"]},
    ))

    assert session.characters[player_id].name == "Rook"


async def test_imported_character_ignored_on_reconnect():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")
    session.characters[player_id].hp = 3  # simulate some real damage taken

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "imported_character": {"name": "Someone Else", "hp": 99, "max_hp": 99}},
    ))

    character = session.characters[player_id]
    assert character.name == "Thrain", "a reconnect must never be overwritten by an import"
    assert character.hp == 3


async def test_out_of_turn_action_is_rejected_not_queued():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=other_id,
        payload={"text": "I do nothing"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings, "out-of-turn action should produce a warning"
    assert session.current_turn == player_id


async def test_action_streams_narration_and_advances_turn():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    narration_chunks = [r for r in received if r[0] == "broadcast" and r[2].get("kind") == "narration"]
    assert narration_chunks, "should have streamed narration chunks"
    assert narration_chunks[-1][2]["done"] is True

    full_text = "".join(c[2]["text"] for c in narration_chunks[:-1])
    assert full_text == "You swing your sword."
    assert session.log[-1]["text"] == full_text
    assert session.current_turn == player_id  # only player seated, turn cycles back to them


async def test_action_updates_history_and_passes_it_to_next_narrate_call():
    dm = StubDM()
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))
    assert dm.calls[0] == [], "first turn should see an empty rolling window"

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I check my inventory"},
    ))
    assert dm.calls[1] == [
        {"role": "user", "content": "I attack the goblin"},
        {"role": "assistant", "content": "You swing your sword."},
    ], "second turn should see the first turn's exchange as history"

    assert session.history == [
        {"role": "user", "content": "I attack the goblin"},
        {"role": "assistant", "content": "You swing your sword."},
        {"role": "user", "content": "I check my inventory"},
        {"role": "assistant", "content": "You swing your sword."},
    ]


async def test_update_character_tool_call_applies_and_pushes_character_update():
    dm = UpdateCharacterDM({"hp_delta": -4, "add_item": "torch"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I grab a torch as the trap fires"},
    ))

    character = session.characters[player_id]
    assert character.hp == 6  # 10 - 4
    assert character.find_item("torch") is not None
    assert "HP -4" in dm.tool_result

    updates = [
        r for r in received
        if r[0] == "send_to" and r[2] == "character_update" and r[1] == player_id
    ]
    assert updates, "a real sheet change should push a character_update to the player"
    assert updates[-1][3]["sheet_delta"]["hp"] == 6
    assert updates[-1][3]["sheet_delta"]["inventory"] == [{"name": "torch", "quantity": 1, "magic_bonus": 0}]


async def test_update_character_rest_heals_the_acting_character_through_a_real_turn():
    dm = UpdateSequenceDM([{"hp_delta": -7}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)  # blank class, hp=10/max_hp=10

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I take a bad hit"},
    ))
    assert session.characters[player_id].hp == 3

    dm._updates = [{"rest": "long"}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I make camp and rest for the night"},
    ))

    assert session.characters[player_id].hp == 10
    assert "long rest" in dm.tool_results[-1]


async def test_update_character_rest_heals_a_tracked_npc_too():
    # apply_update's rest handling lives on CharacterSheet itself, so it
    # applies to a tracked NPC the same way it does the acting character -
    # no separate wiring needed in the NPC-targeting branch.
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 10, "hp_delta": -8}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I wound the goblin"},
    ))
    assert session.npcs["goblin"].hp == 2

    dm._updates = [{"target": "goblin", "rest": "short"}]  # missing 8, +4
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "The goblin retreats and catches its breath"},
    ))

    assert session.npcs["goblin"].hp == 6


async def test_update_character_no_op_does_not_push_character_update():
    dm = UpdateCharacterDM({"hp_delta": 0})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I do something inconsequential"},
    ))

    assert not any(r[0] == "send_to" and r[2] == "character_update" for r in received)


async def test_update_character_npc_target_creates_tracked_npc_with_given_max_hp():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -4}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    assert "goblin" in session.npcs
    goblin = session.npcs["goblin"]
    assert goblin.hp == 3  # 7 - 4
    assert goblin.max_hp == 7
    assert "Introduced goblin" in dm.tool_results[0]
    assert "HP -4" in dm.tool_results[0]

    # session.characters (the player's own sheet) must be untouched.
    assert player_id in session.characters
    assert "goblin" not in session.characters


async def test_update_character_npc_target_defaults_max_hp_when_omitted():
    dm = UpdateSequenceDM([{"target": "rat", "hp_delta": -2}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I stomp the rat"},
    ))

    rat = session.npcs["rat"]
    assert rat.max_hp == 10  # DEFAULT_NPC_HP, same fallback join_session uses
    assert rat.hp == 8


async def test_update_character_npc_target_disposition_persists_and_updates():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "disposition": "hostile"}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    assert session.npcs["goblin"].disposition == "hostile"
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert updates[-1][2]["sheet_delta"]["disposition"] == "hostile"

    dm._updates = [{"target": "goblin", "disposition": "friendly"}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I offer the goblin a truce"},
    ))
    assert session.npcs["goblin"].disposition == "friendly"


async def test_npc_introduction_broadcasts_npc_update():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -4}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert updates, "introducing an NPC should broadcast npc_update"
    assert updates[-1][2]["name"] == "goblin"
    assert updates[-1][2]["sheet_delta"]["hp"] == 3
    assert updates[-1][2]["sheet_delta"]["max_hp"] == 7


async def test_npc_state_persists_and_accumulates_across_turns():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -4}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))
    assert session.npcs["goblin"].hp == 3

    dm._updates = [{"target": "goblin", "hp_delta": -3}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin again"},
    ))

    assert len(session.npcs) == 1, "same-named NPC must be updated, not duplicated"
    assert session.npcs["goblin"].hp == 0  # 3 - 3, clamped at 0 not negative
    assert "Introduced" not in dm.tool_results[-1], "second call updates, doesn't re-introduce"


async def test_npc_no_op_update_does_not_broadcast_again():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -4}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    dm._updates = [{"target": "goblin", "hp_delta": 0}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I glance at the goblin"},
    ))

    npc_updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert len(npc_updates) == 1, "a no-op update to an already-tracked NPC shouldn't rebroadcast"


async def test_npc_target_recased_on_later_turn_updates_existing_npc_not_a_duplicate():
    # ROADMAP.md: a live qwen2.5:7b run called target="Bandit" (capitalized)
    # against a scenario whose narration consistently said "the bandit"
    # (lowercase) - Session.npcs used to be keyed by the exact raw string,
    # so an inconsistently-cased target would silently create a second,
    # disconnected NPC instead of updating the one already tracked.
    dm = UpdateSequenceDM([{"target": "bandit", "max_hp": 10, "hp_delta": -3}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the bandit"},
    ))
    assert session.npcs["bandit"].hp == 7

    dm._updates = [{"target": "Bandit", "hp_delta": -3}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the bandit again"},
    ))

    assert len(session.npcs) == 1, "re-cased target must update the existing NPC, not duplicate it"
    assert session.npcs["bandit"].hp == 4
    assert "Introduced" not in dm.tool_results[-1], "re-cased target updates, doesn't re-introduce"


async def test_npc_update_broadcast_keeps_first_seen_casing_after_recase():
    # The dict key normalizes to casefold for lookup, but the display name
    # broadcast to clients should stay the name the NPC was first introduced
    # with, not whatever casing a later call happens to use - otherwise the
    # same NPC would render under two different labels turn to turn.
    dm = UpdateSequenceDM([{"target": "Bandit", "max_hp": 10, "hp_delta": -3}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the bandit"},
    ))

    dm._updates = [{"target": "bandit", "hp_delta": -3}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the bandit again"},
    ))

    updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert [u[2]["name"] for u in updates] == ["Bandit", "Bandit"]


async def test_defeating_known_srd_monster_awards_correct_cr_xp():
    # "goblin" matches server/rules/srd.json's own monster entry (CR 1/4),
    # so the real SRD xp_by_cr table (50 XP) should apply automatically -
    # no explicit "xp" needed on the killing update.
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin down"},
    ))

    assert session.npcs["goblin"].hp == 0
    assert session.characters[player_id].xp == 50
    assert "is defeated" in dm.tool_results[0]
    assert "gains 50 XP" in dm.tool_results[0]


async def test_defeating_a_newly_added_srd_monster_awards_correct_cr_xp():
    # "bandit" (added this pass, CR 1/8) matches server/rules/srd.json's
    # own monster entry - locks that a newly-added monster integrates with
    # the real xp_by_cr table (25 XP) and copies its real stat block onto
    # the introduced NPC, not just that the data loads.
    dm = UpdateSequenceDM([{"target": "bandit", "max_hp": 11, "hp_delta": -11}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit down"},
    ))

    assert session.npcs["bandit"].hp == 0
    assert session.npcs["bandit"].ac == 12  # copied from the real SRD stat block
    assert session.characters[player_id].xp == 25
    assert "gains 25 XP" in dm.tool_results[0]


async def test_defeating_unmatched_npc_falls_back_to_default_xp():
    dm = UpdateSequenceDM([{"target": "shadow_beast", "max_hp": 5, "hp_delta": -5}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the shadow beast down"},
    ))

    from server.engine import DEFAULT_NPC_XP
    assert session.characters[player_id].xp == DEFAULT_NPC_XP


async def test_explicit_xp_override_takes_precedence_over_cr_lookup():
    # "goblin" would normally resolve to the SRD's 50 XP - an explicit xp
    # on the killing update should win anyway, the same override precedent
    # max_hp already has over DEFAULT_NPC_HP.
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7, "xp": 999}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin down"},
    ))

    assert session.characters[player_id].xp == 999


async def test_no_xp_awarded_for_repeat_hits_on_an_already_dead_npc():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin down"},
    ))
    assert session.characters[player_id].xp == 50

    dm._updates = [{"target": "goblin", "hp_delta": -1}]
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I hit the goblin's corpse"},
    ))

    assert session.characters[player_id].xp == 50, "an already-dead NPC must not re-award XP"
    assert "is defeated" not in dm.tool_results[-1]


async def test_defeat_broadcasts_system_message_announcing_xp():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin down"},
    ))

    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "gains 50 XP" in r[2]["text"]
    ]
    assert announcements, "a defeat should broadcast a system_message announcing the XP gained"


async def test_party_split_awards_even_share_to_every_joined_player():
    # Real 5e's party-wide XP: a 50 XP goblin split between two players is
    # 25 each - the acting player keeps their own share, the other player
    # gets the rest, and the announcement names the whole party.
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")
    await join(engine, other_id, name="Lyra")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin down"},
    ))

    assert session.characters[player_id].xp == 25
    assert session.characters[other_id].xp == 25
    assert "The party gains 50 XP" in dm.tool_results[0]
    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message"
        and "The party defeats goblin and gains 50 XP (25 each)" in r[2]["text"]
    ]
    assert announcements, "a party defeat should announce the per-member share"
    updates = [r for r in received if r[0] == "send_to" and r[2] == "character_update"]
    assert {r[3]["player_id"] for r in updates} == {player_id, other_id}


async def test_party_split_drops_the_remainder():
    # A 50 XP kill across three players floors to 16 each (48 total) - the
    # leftover 2 simply drops, real 5e's "divvy evenly" guidance.
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -7}])
    engine, session, _ = make_engine(dm)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for pid in ids:
        await join(engine, pid)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=ids[0],
        payload={"text": "I strike the goblin down"},
    ))

    assert all(session.characters[pid].xp == 16 for pid in ids)


async def test_party_split_levels_up_every_member_who_crosses_the_threshold():
    # A 5,400 XP kill split across two players is 2,700 each - exactly the
    # level-4 threshold, so both members level and both get their ASI, and
    # the broadcast announces each leveler by name.
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 5400}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    for pid, name in ((player_id, "Thrain"), (other_id, "Lyra")):
        await engine.handle(Envelope(
            type="join_session", session_id="test-session", sender_id=pid,
            payload={"player_name": name, "character_class": "fighter"},
        ))

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert session.characters[player_id].xp == 2700
    assert session.characters[other_id].xp == 2700
    assert session.characters[player_id].level == 4
    assert session.characters[other_id].level == 4
    assert session.characters[player_id].stats["str"] == 17
    assert session.characters[other_id].stats["str"] == 17
    text = "".join(
        r[2]["text"] for r in received if r[0] == "broadcast" and r[1] == "system_message"
    )
    assert "Thrain reaches level 4" in text
    assert "Lyra reaches level 4" in text


async def test_level_up_grows_hp_by_class_hit_die_and_broadcasts_level_up():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 300}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]
    assert character.level == 1
    assert character.max_hp == 12  # fighter's d10 hit die max (10) + CON modifier (+2)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.xp == 300  # exactly the level-2 threshold
    assert character.level == 2
    assert character.max_hp == 24  # +12 (fighter's d10 max + CON mod) for the level gained
    assert character.hp == 24

    level_ups = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "reaches level 2" in r[2]["text"]
    ]
    assert level_ups, "leveling up should broadcast an announcement"


async def test_level_up_with_no_known_class_does_not_grow_hp():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 300}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)  # no character_class - blank class, no hit_die to grow by
    character = session.characters[player_id]
    starting_max_hp = character.max_hp

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.level == 2
    assert character.max_hp == starting_max_hp


async def test_level_4_grants_an_ability_score_improvement_to_the_top_priority_ability():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 2700}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]
    assert character.stats["str"] == 15  # fighter's Standard Array starting STR

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.level == 4
    assert character.stats["str"] == 17  # +2 - fighter's own top CLASS_ABILITY_PRIORITY entry

    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "STR increases" in r[2]["text"]
    ]
    assert announcements


async def test_level_3_grants_no_ability_score_improvement():
    # 900 XP is exactly the level-3 threshold - not an ASI level, so stats
    # should be completely untouched, unlike the level-4 case above.
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 900}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.level == 3
    assert character.stats["str"] == 15


async def test_asi_falls_through_to_next_priority_ability_when_the_top_one_is_capped():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 2700}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]
    character.stats["str"] = 20  # already at the real cap

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.stats["str"] == 20  # untouched - already capped
    assert character.stats["con"] == 16  # fighter's second-priority ability instead (+2 from 14)

    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "CON increases" in r[2]["text"]
    ]
    assert announcements


async def test_asi_never_exceeds_the_real_20_cap():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 2700}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]
    character.stats["str"] = 19  # a +2 would overshoot 20 if not capped

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.stats["str"] == 20  # capped, not 21


async def test_asi_crossing_two_thresholds_in_one_award_applies_both():
    # A single huge XP award (level 1 straight to level 8) crosses both
    # ASI level 4 and level 8 - CharacterSheet.gain_xp()'s own level-up
    # loop already handles crossing more than one XP threshold at once;
    # this confirms ability score improvements track the same real
    # per-level crossings, not just "did level change at all".
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 34000}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.level == 8
    assert character.stats["str"] == 19  # 15 + 2 (level 4) + 2 (level 8)

    # Deduplicated announcement text, not "STR and STR increase!" - see
    # _asi_announcement's own docstring for why.
    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "STR increases" in r[2]["text"]
    ]
    assert announcements
    assert "STR and STR" not in announcements[-1][2]["text"]


async def test_asi_does_not_apply_with_no_known_class():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 2700}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)  # no character_class - no stats to improve at all
    character = session.characters[player_id]

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    assert character.level == 4
    assert character.stats == {}


def test_apply_ability_score_improvements_no_asi_level_crossed_is_a_no_op():
    character = build_starting_character("p1", "Thrain", "fighter", RulesIndex.load_default())
    improved = _apply_ability_score_improvements(character, old_level=1, new_level=3)
    assert improved == []
    assert character.stats["str"] == 15


def test_apply_ability_score_improvements_blank_class_returns_empty():
    character = build_starting_character("p1", "Thrain", "", RulesIndex.load_default())
    improved = _apply_ability_score_improvements(character, old_level=1, new_level=4)
    assert improved == []


def test_asi_announcement_empty_for_no_abilities():
    assert _asi_announcement("Thrain", []) == ""


def test_asi_announcement_singular_for_one_ability():
    text = _asi_announcement("Thrain", ["str"])
    assert text == " Thrain's STR increases!"


def test_asi_announcement_plural_for_two_distinct_abilities():
    text = _asi_announcement("Thrain", ["str", "con"])
    assert text == " Thrain's STR and CON increase!"


def test_asi_announcement_dedupes_the_same_ability_twice():
    text = _asi_announcement("Thrain", ["str", "str"])
    assert text == " Thrain's STR increases!"


async def test_public_character_view_includes_level_but_not_xp():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].xp = 42

    other_id = str(uuid.uuid4())
    await join(engine, other_id, name="Rowan")

    syncs = [r for r in received if r[0] == "send_to" and r[1] == other_id and r[2] == "state_sync"]
    view = syncs[-1][3]["characters"][player_id]
    assert view["level"] == 1
    assert "xp" not in view


async def test_owner_character_view_includes_class_features_and_skill_proficiencies():
    # ROADMAP.md item 7 - the tabbed character sheet needed this data sent
    # at all, not just computed server-side. Both already existed before
    # this (level_1_features in server/rules/srd.json, CLASS_SKILL_
    # PROFICIENCIES in this module) but neither was ever part of any
    # payload a client actually receives.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Elowen", "character_class": "wizard"},
    ))

    view = _owner_character_view(session.characters[player_id], engine._rules)
    assert "Arcane Recovery" in " ".join(view["class_features"])
    assert view["skill_proficiencies"] == ["arcana", "investigation"]


async def test_owner_character_view_still_includes_everything_model_dump_has():
    # A thin wrapper, not a replacement - the owner's own state_sync/
    # character_update payloads shouldn't lose any existing field (hp,
    # inventory, xp, ...) just because two new ones were added on top.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    view = _owner_character_view(session.characters[player_id], engine._rules)
    assert view["hp"] == session.characters[player_id].hp
    assert "xp" in view


async def test_owner_character_view_handles_a_blank_or_unrecognized_class():
    # No class_entry in the SRD dataset for "" or an unrecognized class -
    # the same graceful "not present isn't an error" fallback
    # CLASS_ABILITY_PRIORITY's own absence already produces for stats,
    # not a KeyError.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    view = _owner_character_view(session.characters[player_id], engine._rules)
    assert view["class_features"] == []
    assert view["skill_proficiencies"] == []


async def test_owner_character_view_class_features_grow_with_level():
    # srd.json's features_by_level accumulates through the character's own
    # level - derived at view time from (class, level), so a real level-up
    # automatically reveals what it granted.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))
    character = session.characters[player_id]

    feats = " ".join(_owner_character_view(character, engine._rules)["class_features"])
    assert "Second Wind" in feats
    assert "Action Surge" not in feats

    character.gain_xp(300, engine._rules.xp_thresholds())  # -> level 2
    feats = " ".join(_owner_character_view(character, engine._rules)["class_features"])
    assert "Action Surge" in feats
    assert "Extra Attack" not in feats

    character.gain_xp(6200, engine._rules.xp_thresholds())  # -> level 5
    assert character.level == 5
    feats = " ".join(_owner_character_view(character, engine._rules)["class_features"])
    assert "Extra Attack" in feats
    assert "Indomitable" not in feats  # level 9


async def test_owner_character_view_multi_level_jump_includes_every_feature_through_the_new_level():

    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Mirna", "character_class": "cleric"},
    ))
    character = session.characters[player_id]

    character.gain_xp(34000, engine._rules.xp_thresholds())  # 0 -> level 8
    assert character.level == 8
    feats = " ".join(_owner_character_view(character, engine._rules)["class_features"])
    for feature in ("Channel Divinity", "Turn Undead", "Destroy Undead"):
        assert feature in feats
    assert "Divine Intervention" not in feats  # level 10


async def test_owner_character_view_class_features_stay_empty_for_a_leveled_blank_class():
    # A blank/unrecognized class has no progression table to draw from -
    # leveling up must not conjure features out of nowhere.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    character = session.characters[player_id]

    character.gain_xp(300, engine._rules.xp_thresholds())  # -> level 2
    assert character.level == 2
    assert _owner_character_view(character, engine._rules)["class_features"] == []


async def test_owner_character_view_includes_racial_traits():
    # Real 5e SRD racial trait text (server/rules/srd.json's "races" table)
    # - existed as data but was never attached to any payload before the
    # race system, the same gap class_features closed for classes.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Elowen", "character_class": "wizard", "race": "elf"},
    ))

    view = _owner_character_view(session.characters[player_id], engine._rules)
    assert "Fey Ancestry" in " ".join(view["racial_traits"])


async def test_owner_character_view_includes_subrace_traits_combined_with_base_race():
    # high_elf's own traits already include elf's base traits (Darkvision,
    # Fey Ancestry, ...) plus its own subrace-specific ones (Cantrip,
    # Elf Weapon Training) - one self-contained entry, not two merged at
    # request time.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Elowen", "character_class": "wizard", "race": "high_elf"},
    ))

    view = _owner_character_view(session.characters[player_id], engine._rules)
    traits_text = " ".join(view["racial_traits"])
    assert "Fey Ancestry" in traits_text  # base elf trait
    assert "Cantrip" in traits_text  # high elf's own subrace trait


async def test_owner_character_view_handles_a_blank_or_unrecognized_race():
    # No race_entry in the SRD dataset for a character who never picked
    # one - the same graceful "not present isn't an error" fallback
    # class_features already has for a blank/unrecognized class.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    view = _owner_character_view(session.characters[player_id], engine._rules)
    assert view["racial_traits"] == []


async def test_state_sync_sends_owner_view_with_class_features_to_the_owner_only():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Elowen", "character_class": "wizard"},
    ))

    other_id = str(uuid.uuid4())
    await join(engine, other_id, name="Rowan")

    # Elowen's own state_sync: her own entry is the owner view.
    own_syncs = [r for r in received if r[0] == "send_to" and r[1] == player_id and r[2] == "state_sync"]
    own_view = own_syncs[-1][3]["characters"][player_id]
    assert "Arcane Recovery" in " ".join(own_view["class_features"])

    # Rowan's state_sync: Elowen's entry there is the *public* view - same
    # inventory/stats/notes privacy boundary _public_character_view already
    # draws, just extended to the two fields this test adds.
    others_syncs = [r for r in received if r[0] == "send_to" and r[1] == other_id and r[2] == "state_sync"]
    elowen_as_seen_by_rowan = others_syncs[-1][3]["characters"][player_id]
    assert "class_features" not in elowen_as_seen_by_rowan
    assert "skill_proficiencies" not in elowen_as_seen_by_rowan


async def test_update_character_explicit_self_target_still_updates_own_sheet():
    dm = UpdateCharacterDM({"target": "self", "hp_delta": -1})
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I stub my toe"},
    ))

    assert session.characters[player_id].hp == 9
    assert session.npcs == {}


async def test_update_character_target_matching_own_player_id_treated_as_self():
    # Live testing against llama3.1:8b found the model sometimes echoes the
    # literal player_id from the character summary JSON as target instead of
    # "self" - this should still land on the real sheet, not spawn a phantom
    # NPC named after the player_id.
    player_id = str(uuid.uuid4())
    dm = UpdateCharacterDM({"target": player_id, "hp_delta": -3})
    engine, session, _ = make_engine(dm)
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I take a hit"},
    ))

    assert session.characters[player_id].hp == 7
    assert session.npcs == {}


async def test_update_character_target_matching_own_name_treated_as_self():
    # Live testing of the two-request split (ROADMAP.md item 6) found the
    # same self-identification bug in a second shape: the model echoing the
    # character's own *name* (also present in the character summary JSON) as
    # target instead of "self".
    player_id = str(uuid.uuid4())
    dm = UpdateCharacterDM({"target": "Thrain", "hp_delta": -3})
    engine, session, _ = make_engine(dm)
    await join(engine, player_id, name="Thrain")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I take a hit"},
    ))

    assert session.characters[player_id].hp == 7
    assert session.npcs == {}


async def test_update_character_target_matching_own_condition_treated_as_self():
    # A real, live-found gap (ROADMAP.md's wander campaign dry-run,
    # 2026-08-10): the acting character's own applied condition
    # ("Veil-Touched", every new character's origin condition) got echoed
    # back as target instead of "self" - a third shape of the same
    # self-identification confusion player_id/name misrouting already
    # guards against. Left unguarded, this spawned a bogus phantom NPC
    # literally named "Veil-Touched" that then polluted /combat start's
    # initiative order.
    dm = UpdateSequenceDM([
        {"target": "self", "add_condition": "Veil-Touched"},
        {"target": "Veil-Touched", "hp_delta": -2},
    ])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "Something lunges out of the shadows"},
    ))

    character = session.characters[player_id]
    assert character.hp == 8
    assert character.conditions == ["Veil-Touched"]
    assert session.npcs == {}


async def test_state_sync_includes_npcs_for_a_later_joining_player():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -4}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    second_player_id = str(uuid.uuid4())
    await join(engine, second_player_id, name="Rowan")

    syncs = [
        r for r in received
        if r[0] == "send_to" and r[1] == second_player_id and r[2] == "state_sync"
    ]
    assert syncs, "the second player should get a state_sync on join"
    assert syncs[-1][3]["npcs"]["goblin"]["hp"] == 3


async def test_join_broadcasts_player_joined_with_public_view_only():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))

    joins = [r for r in received if r[0] == "broadcast" and r[1] == "player_joined"]
    assert joins, "joining should broadcast a structured player_joined event"
    payload = joins[-1][2]
    assert payload["player_id"] == player_id
    assert payload["name"] == "Rook"
    assert payload["character_class"] == "Fighter"
    assert payload["hp"] == payload["max_hp"] == 12  # d10 hit die max (10) + CON modifier (+2)
    assert payload["conditions"] == []
    # A fighter starts with real inventory (Longsword, Leather Armor) - the
    # public view must never leak it, or anyone's own stats/notes.
    assert "inventory" not in payload
    assert "stats" not in payload
    assert "notes" not in payload


async def test_second_players_state_sync_redacts_first_players_inventory():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))

    second_player_id = str(uuid.uuid4())
    await join(engine, second_player_id, name="Rowan")

    syncs = [
        r for r in received
        if r[0] == "send_to" and r[1] == second_player_id and r[2] == "state_sync"
    ]
    others_view = syncs[-1][3]["characters"][player_id]
    assert others_view["name"] == "Rook"
    assert others_view["hp"] == others_view["max_hp"] == 12  # d10 hit die max (10) + CON modifier (+2)
    assert "inventory" not in others_view, "another player's inventory must never reach a non-owning client"
    assert "stats" not in others_view
    assert "notes" not in others_view

    # The second player's own entry in their own sync is the full sheet,
    # not redacted against themselves.
    own_view = syncs[-1][3]["characters"][second_player_id]
    assert "inventory" in own_view


async def test_sheet_change_broadcasts_public_player_update_alongside_private_character_update():
    dm = UpdateCharacterDM({"target": "self", "hp_delta": -3, "add_item": "torch"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I light a torch and take a hit"},
    ))

    private_updates = [
        r for r in received if r[0] == "send_to" and r[1] == player_id and r[2] == "character_update"
    ]
    assert private_updates
    assert private_updates[-1][3]["sheet_delta"]["inventory"] == [{"name": "torch", "quantity": 1, "magic_bonus": 0}]

    public_updates = [r for r in received if r[0] == "broadcast" and r[1] == "player_update"]
    assert public_updates, "a sheet change should also broadcast the public view to everyone else"
    payload = public_updates[-1][2]
    assert payload["player_id"] == player_id
    assert payload["hp"] == 7
    assert "inventory" not in payload


async def test_handle_disconnect_broadcasts_player_left():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")

    await engine.handle_disconnect(player_id)

    left = [r for r in received if r[0] == "broadcast" and r[1] == "player_left"]
    assert left
    assert left[-1][2] == {"player_id": player_id, "name": "Thrain"}


async def test_handle_disconnect_for_never_joined_player_falls_back_to_id_as_name():
    # Defensive path - the transport only tracks a connection after a real
    # join_session, so this shouldn't happen in practice, but shouldn't
    # crash if it somehow did.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle_disconnect(player_id)

    left = [r for r in received if r[0] == "broadcast" and r[1] == "player_left"]
    assert left[-1][2] == {"player_id": player_id, "name": player_id}


async def test_rejoin_uses_existing_character_name_not_new_input():
    """Regression test: rejoining with a name typed differently than the
    original (e.g. a fresh client run before .player_id existed, or a stale
    prompt) must not rename the character or misreport it in the join
    broadcast."""
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")

    await join(engine, player_id, name="SomeoneElse")

    assert session.characters[player_id].name == "Thrain"
    joins = [r for r in received if r[0] == "broadcast" and r[1] == "system_message"]
    assert joins[-1][2]["text"] == "Thrain joined the session."


async def test_rejoin_with_a_different_typed_name_gets_a_private_heads_up():
    """A player typing a name that doesn't match their existing character
    (e.g. a stale local .player_id from a previous session) used to have no
    way of knowing their input was silently ignored - a real, found-live
    point of confusion, not a hypothetical one. This is private
    (send_to), not broadcast - it's about what *this* player should
    understand about their own reconnect, not news for everyone else."""
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")
    received.clear()

    await join(engine, player_id, name="SomeoneElse")

    notices = [
        r for r in received
        if r[0] == "send_to" and r[1] == player_id and r[2] == "system_message" and "reconnecting" in r[3]["text"]
    ]
    assert len(notices) == 1
    assert "Thrain" in notices[0][3]["text"]
    assert "SomeoneElse" in notices[0][3]["text"]


async def test_rejoin_with_the_same_name_gets_no_heads_up():
    """The heads-up above only fires when it'd actually be surprising - a
    plain reconnect (blank name, or the same name typed again) shouldn't
    get a pointless notice about nothing having changed."""
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")
    received.clear()

    await join(engine, player_id, name="Thrain")

    assert not any(
        r[0] == "send_to" and r[2] == "system_message" and "reconnecting" in r[3]["text"] for r in received
    )


async def test_dice_roll_broadcasts_result_regardless_of_turn():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id)  # player_id now holds the only turn

    # other_id never joined and isn't the seated player — dice rolling is
    # exempt from turn order, unlike player_action.
    await engine.handle(Envelope(
        type="dice_roll", session_id="test-session", sender_id=other_id,
        payload={"dice": "1d20", "reason": "stealth check"},
    ))

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert dice_logs, "dice roll should produce a log entry"
    assert "stealth check" in dice_logs[-1][2]["text"]

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results, "dice roll should broadcast a dice_result"
    payload = results[-1][2]
    assert payload["roller_id"] == other_id
    assert 1 <= payload["result"] <= 20


async def test_invalid_dice_notation_warns_sender_only():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="dice_roll", session_id="test-session", sender_id=player_id,
        payload={"dice": "not-dice"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert not any(r[0] == "broadcast" and r[1] == "dice_result" for r in received)


async def test_dm_requested_roll_with_dc_broadcasts_success_verdict():
    dm = RequestRollDM({"dice": "1d20+2", "dc": 12, "reason": "attack roll"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results, "a DM-requested roll should broadcast a dice_result"
    payload = results[-1][2]
    assert payload["roller_id"] == player_id
    assert payload["dc"] == 12
    assert payload["success"] in (True, False)
    assert payload["success"] == (payload["result"] >= 12)

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert dice_logs, "a DM-requested roll should also produce a visible log line"
    assert "vs DC 12" in dice_logs[-1][2]["text"]
    assert ("success" in dice_logs[-1][2]["text"]) or ("failure" in dice_logs[-1][2]["text"])

    assert dm.tool_result is not None and "DC 12" in dm.tool_result


async def test_dm_requested_roll_without_dc_has_no_success_verdict():
    dm = RequestRollDM({"dice": "2d6", "reason": "damage roll"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I roll damage"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "dc" not in payload
    assert "success" not in payload

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "vs DC" not in dice_logs[-1][2]["text"]


async def test_dm_requested_roll_with_ability_applies_real_modifier_automatically():
    # fighter's real stats (see CLASS_ABILITY_PRIORITY/STANDARD_ARRAY):
    # str 15(+2), con 14(+2), dex 13(+1), wis 12(+1), cha 10(+0), int 8(-1).
    dm = RequestRollDM({"dice": "1d20", "dc": 12, "reason": "dexterity check", "ability": "dex"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I tumble past the guard"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["ability"] == "dex"
    assert payload["ability_modifier"] == 1
    # The modifier is genuinely added to the total, not just displayed -
    # result should be exactly the raw d20 roll plus the +1 DEX modifier.
    assert payload["result"] == sum(payload["rolls"]) + 1

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "+1 DEX" in dice_logs[-1][2]["text"]
    assert dm.tool_result is not None and "+1 DEX" in dm.tool_result


async def test_dm_requested_roll_applies_disadvantage_from_a_tracked_condition():
    dm = RequestRollDM({"dice": "1d20", "dc": 10, "reason": "stealth check"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("poisoned")

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I try to sneak past"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["disadvantage"] is True
    assert payload["disadvantage_reasons"] == ["poisoned"]
    assert payload["rolls"] == [18, 4]
    assert payload["result"] == 4  # the kept (lower) roll, not the discarded 18

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "disadvantage: poisoned" in dice_logs[-1][2]["text"]
    assert dm.tool_result is not None and "disadvantage: poisoned" in dm.tool_result


async def test_dm_requested_roll_matches_conditions_case_insensitively():
    dm = RequestRollDM({"dice": "1d20"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("Frightened")  # DM-typed casing varies

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I try to act despite my fear"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["disadvantage"] is True


async def test_dm_requested_roll_multiple_disadvantage_conditions_still_only_apply_once():
    # Real 5e disadvantage never stacks - this locks that the mechanic
    # itself doesn't double-roll or otherwise behave differently with two
    # qualifying conditions present, while still naming both as the reason.
    dm = RequestRollDM({"dice": "1d20"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.extend(["poisoned", "frightened"])

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I push forward anyway"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["disadvantage_reasons"] == ["poisoned", "frightened"]
    assert len(payload["rolls"]) == 2  # still just one roll-twice, not two


async def test_request_roll_natural_20_attack_is_a_critical_hit():
    dm = RequestRollDM({"dice": "1d20", "dc": 15, "roll_kind": "attack", "reason": "sword swing"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    with patch("server.dice.random.randint", return_value=20):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I swing my sword"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["critical"] is True
    assert dm.tool_result is not None and "CRITICAL HIT!" in dm.tool_result

    # The broadcast plain-text log line (GameEngine._dice_log_text) must
    # show the same critical callout the DM's own tool_result does - a
    # real, previously-latent gap: _dice_log_text used to be a second,
    # independent label-builder that never carried this at all, found and
    # fixed while unifying it with request_roll's own copy.
    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "CRITICAL HIT!" in dice_logs[-1][2]["text"]


async def test_request_roll_natural_20_on_a_check_is_not_a_critical_hit():
    # Real 5e's critical-hit rule only applies to attack rolls - a great
    # skill check isn't a "critical hit", even on a natural 20.
    dm = RequestRollDM({"dice": "1d20", "dc": 15, "roll_kind": "check", "reason": "perception"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    with patch("server.dice.random.randint", return_value=20):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I look around carefully"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "critical" not in results[-1][2]


async def test_request_roll_natural_20_on_a_non_d20_attack_notation_is_not_a_critical_hit():
    # A weapon-damage-shaped roll mislabeled roll_kind="attack" shouldn't
    # falsely read as a crit just because some die happened to max out -
    # real 5e's crit rule is specifically about the d20 to-hit roll.
    dm = RequestRollDM({"dice": "2d6", "roll_kind": "attack", "reason": "damage"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    with patch("server.dice.random.randint", return_value=6):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I roll damage"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "critical" not in results[-1][2]


async def test_request_roll_critical_uses_the_kept_roll_under_disadvantage():
    # A natural 20 that got discarded under disadvantage was never really
    # rolled as far as the character's outcome is concerned - mirrors the
    # same kept-vs-discarded narrowing the disadvantage tests above lock
    # for highlighting, applied to crit detection instead.
    dm = RequestRollDM({"dice": "1d20", "dc": 15, "roll_kind": "attack", "reason": "sword swing"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("poisoned")

    with patch("server.dice.random.randint", side_effect=[20, 5]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I swing my sword despite the poison"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["result"] == 5  # the kept (lower) roll
    assert "critical" not in payload  # the discarded 20 doesn't count


@pytest.mark.parametrize(
    "update,expected",
    [
        ({"hp_delta": -5}, "damage"),
        ({"hp_delta": 5}, "heal"),
        ({"rest": "long"}, "heal"),
        ({"add_condition": "poisoned"}, "condition"),
        ({"remove_condition": "poisoned"}, "condition"),
        ({"cast_spell": "fire_bolt"}, "spell"),
        ({"add_item": "a torch"}, "item"),
        ({"remove_item": "a torch"}, "item"),
        ({"notes": "just bookkeeping"}, None),
        ({"disposition": "hostile"}, None),
        ({}, None),
        # hp_delta takes priority over a condition in the same call - the
        # most narratively dominant outcome, real 5e's own "a poisoned
        # dart" shape (damage + a condition applied in one hit).
        ({"hp_delta": -3, "add_condition": "poisoned"}, "damage"),
    ],
)
def test_outcome_category(update, expected):
    assert _outcome_category(update) == expected


async def test_update_character_hp_delta_broadcasts_a_colored_outcome_line():
    dm = UpdateCharacterDM({"hp_delta": -5})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I take a hit"},
    ))

    outcomes = [r for r in received if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "outcome"]
    assert outcomes, "a real hp_delta should broadcast a colored outcome line"
    assert outcomes[-1][2]["category"] == "damage"
    assert "HP -5" in outcomes[-1][2]["text"]
    assert outcomes[-1][2]["text"].startswith("Thrain:")


async def test_update_character_notes_only_change_does_not_broadcast_an_outcome_line():
    # notes/disposition-only changes have no dedicated color category
    # (_outcome_category returns None) - shouldn't spam the log with an
    # uncategorized line for bookkeeping that isn't a mechanical outcome.
    dm = UpdateCharacterDM({"notes": "the old man owes me a favor"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I recall a debt owed"},
    ))

    outcomes = [r for r in received if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "outcome"]
    assert not outcomes


async def test_update_character_npc_damage_broadcasts_a_colored_outcome_line_named_for_the_npc():
    dm = UpdateCharacterDM({"target": "goblin", "max_hp": 7, "hp_delta": -3})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the goblin"},
    ))

    outcomes = [r for r in received if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "outcome"]
    assert outcomes
    assert outcomes[-1][2]["category"] == "damage"
    assert outcomes[-1][2]["text"].startswith("goblin:")


async def test_dm_requested_roll_with_a_non_disadvantage_condition_is_unaffected():
    # grappled has no self-roll effect in the real SRD text (only a
    # speed-0 movement effect Oracle doesn't model) - a real, deliberate
    # exclusion, not an oversight.
    dm = RequestRollDM({"dice": "1d20"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("grappled")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to break free"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "disadvantage" not in payload
    assert len(payload["rolls"]) == 1


async def test_roll_kind_save_excludes_poisoned_from_disadvantage():
    # Real SRD text: poisoned gives disadvantage on attack rolls and
    # ability checks - saving throws are never mentioned, so a roll_kind
    # of "save" should not trigger it, unlike every prior test above
    # (all omit roll_kind, so they still get the broader legacy behavior).
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "save"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("poisoned")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I resist the poison's grip on my mind"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "disadvantage" not in payload
    assert len(payload["rolls"]) == 1


async def test_roll_kind_attack_still_applies_poisoned_disadvantage():
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "attack"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("poisoned")

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I swing my sword despite the poison"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["disadvantage"] is True


async def test_roll_kind_save_excludes_frightened_from_disadvantage():
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "save"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("frightened")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I steel my nerve"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "disadvantage" not in results[-1][2]


async def test_roll_kind_check_excludes_prone_from_disadvantage():
    # Prone's real SRD text only ever mentions attack rolls - unlike
    # poisoned/frightened, it doesn't affect checks either, so this
    # excludes a *broader* set of roll kinds than the save-only cases above.
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "check"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("prone")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to recall a useful fact while down"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "disadvantage" not in results[-1][2]


async def test_roll_kind_attack_still_applies_prone_disadvantage():
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "attack"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("prone")

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I attack from the ground"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["disadvantage"] is True


async def test_roll_kind_omitted_keeps_the_prior_broader_disadvantage_behavior():
    # Backward compatibility: a request_roll call that doesn't set
    # roll_kind at all (an older call shape, or a DM that just didn't
    # bother) still gets the original "applies to any roll" behavior -
    # this is additive, not a breaking change to existing callers.
    dm = RequestRollDM({"dice": "1d20"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("poisoned")

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I try to save against the poison"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["disadvantage"] is True


async def test_roll_kind_unrecognized_value_is_treated_as_omitted():
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "bogus"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I roll for something"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "roll_kind" not in results[-1][2]


async def test_roll_kind_appears_in_dice_result_and_tool_result_when_given():
    dm = RequestRollDM({"dice": "1d20", "roll_kind": "check", "reason": "spot a trap"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look for traps"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["roll_kind"] == "check"
    assert dm.tool_result is not None and "(check)" in dm.tool_result


async def test_roll_kind_absent_when_omitted():
    dm = RequestRollDM({"dice": "1d20"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I roll for something"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "roll_kind" not in results[-1][2]


async def _join_as_fighter(engine, player_id, name="Thrain"):
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": name, "character_class": "fighter"},
    ))


async def test_request_roll_skill_adds_proficiency_bonus_when_proficient():
    # Fighter is proficient in athletics (CLASS_SKILL_PROFICIENCIES) -
    # governed by STR, which a fresh fighter has at 15 (+2 modifier).
    dm = RequestRollDM({"dice": "1d20", "skill": "athletics"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to climb the wall"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["skill"] == "athletics"
    assert payload["proficient"] is True
    assert payload["proficiency_bonus"] == 2  # level 1
    assert payload["ability"] == "str"  # resolved automatically from the skill
    assert payload["ability_modifier"] == 2
    assert payload["result"] == payload["rolls"][0] + 2 + 2  # both the STR mod and proficiency


async def test_request_roll_skill_no_bonus_when_not_proficient():
    # Stealth isn't in the fighter's own CLASS_SKILL_PROFICIENCIES.
    dm = RequestRollDM({"dice": "1d20", "skill": "stealth"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to sneak past"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["skill"] == "stealth"
    assert payload["proficient"] is False
    assert "proficiency_bonus" not in payload
    assert payload["ability"] == "dex"  # still auto-resolved, just no proficiency added


async def test_request_roll_save_adds_proficiency_bonus_when_class_is_proficient():
    # Fighter is proficient in Str/Con saves (CLASS_SAVING_THROW_PROFICIENCIES,
    # matching the SRD's own real class saving_throws data) - a fresh
    # fighter has CON 14 (+2 modifier).
    dm = RequestRollDM({"dice": "1d20", "ability": "con", "roll_kind": "save"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I brace against the poison"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["roll_kind"] == "save"
    assert payload["proficient"] is True
    assert payload["proficiency_bonus"] == 2  # level 1
    assert payload["result"] == payload["rolls"][0] + 2 + 2  # both the CON mod and proficiency

    # The broadcast plain-text log line must show the same "+N proficiency"
    # tag the dice_result payload already carries - a real, previously-
    # latent gap: _dice_log_text's own roll_kind_label never had this
    # save-proficiency special case at all, found and fixed while
    # unifying it with request_roll's own copy (which already had it).
    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "(save, +2 proficiency)" in dice_logs[-1][2]["text"]


async def test_request_roll_save_no_bonus_when_class_not_proficient():
    # Wisdom saves aren't in the fighter's own Str/Con pair.
    dm = RequestRollDM({"dice": "1d20", "ability": "wis", "roll_kind": "save"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I resist the illusion"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["roll_kind"] == "save"
    assert payload["proficient"] is False
    assert "proficiency_bonus" not in payload


async def test_request_roll_matching_ability_without_save_roll_kind_gets_no_proficiency():
    # A fighter's own CON is a proficient save, but only when the DM
    # actually marks the roll a save (roll_kind: "save") - the same
    # ability used for a plain check/no roll_kind shouldn't silently gain
    # saving-throw proficiency it was never asked for.
    dm = RequestRollDM({"dice": "1d20", "ability": "con"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to hold my breath"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload.get("roll_kind") is None
    assert "proficient" not in payload
    assert "proficiency_bonus" not in payload


async def test_request_roll_skill_defaults_roll_kind_to_check():
    # Naming a skill implies roll_kind="check" automatically, which in
    # turn gets the real per-condition disadvantage scoping for free -
    # prone excludes "check" (see ROLL_KIND_DISADVANTAGE_EXCLUSIONS).
    dm = RequestRollDM({"dice": "1d20", "skill": "athletics"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)
    session.characters[player_id].conditions.append("prone")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to climb the wall despite being prone"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["roll_kind"] == "check"
    assert "disadvantage" not in payload  # prone doesn't affect checks, only attacks


async def test_request_roll_unrecognized_skill_is_a_graceful_no_op():
    dm = RequestRollDM({"dice": "1d20", "skill": "juggling"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attempt something odd"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "skill" not in payload
    assert "ability" not in payload  # nothing to auto-resolve from an unrecognized skill


async def test_request_roll_explicit_ability_overrides_skill_derived_ability():
    # A real, if rare, 5e case - some rolls swap a skill's usual ability.
    # Explicit DM intent wins over the automatic default.
    dm = RequestRollDM({"dice": "1d20", "skill": "athletics", "ability": "dex"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as_fighter(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try an athletic feat of agility"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["ability"] == "dex"  # explicit override, not athletics' real "str"
    assert payload["proficient"] is True  # proficiency itself is unaffected by the ability swap


async def test_cast_spell_consumes_a_real_slot_and_broadcasts_character_update():
    dm = UpdateCharacterDM({"cast_spell": "Magic Missile"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")
    assert session.characters[player_id].spell_slots == {"1": 2}

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast magic missile"},
    ))

    character = session.characters[player_id]
    assert character.spell_slots == {"1": 1}
    assert dm.tool_result is not None and "Magic Missile" in dm.tool_result
    updates = [r for r in received if r[0] == "send_to" and r[2] == "character_update"]
    assert updates, "a real slot spend should push a character_update"


async def test_cast_spell_consumes_a_slot_for_a_newly_added_spell():
    # burning_hands (added alongside misty_step for wizard) is a real
    # leveled spell, not just data sitting in srd.json unreachable by any
    # character - this locks that it's actually in CLASS_KNOWN_SPELLS and
    # castable, the same way Magic Missile already is above.
    dm = UpdateCharacterDM({"cast_spell": "Burning Hands"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast burning hands"},
    ))

    character = session.characters[player_id]
    assert character.spell_slots == {"1": 1}
    assert dm.tool_result is not None and "Burning Hands" in dm.tool_result


async def test_cast_spell_consumes_a_slot_for_a_third_batch_first_level_spell():
    # Guiding Bolt (2026-08-22 batch, cleric's first ranged attack spell)
    # - same real-CLASS_KNOWN_SPELLS lock as Burning Hands above.
    dm = UpdateCharacterDM({"cast_spell": "Guiding Bolt"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "cleric", name="Fenwick")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast guiding bolt"},
    ))

    character = session.characters[player_id]
    assert character.spell_slots == {"1": 1}
    assert dm.tool_result is not None and "Guiding Bolt" in dm.tool_result


async def test_cast_spell_consumes_a_slot_for_a_third_batch_second_level_spell():
    # Hold Person is 2nd level - a fresh level-1 caster has no 2nd-level
    # slot yet, so this grants one directly (mirroring a real level-up)
    # to confirm the spell itself is real and reachable, not just data
    # sitting in srd.json.
    dm = UpdateCharacterDM({"cast_spell": "Hold Person"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")
    session.characters[player_id].spell_slots["2"] = 1

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast hold person"},
    ))

    character = session.characters[player_id]
    assert character.spell_slots["2"] == 0
    assert dm.tool_result is not None and "Hold Person" in dm.tool_result


async def test_cast_spell_cantrip_does_not_consume_a_slot():
    dm = UpdateCharacterDM({"cast_spell": "fire_bolt"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast fire bolt"},
    ))

    assert session.characters[player_id].spell_slots == {"1": 2}  # untouched
    assert dm.tool_result is not None and "cantrip" in dm.tool_result


async def test_cast_spell_unrecognized_name_is_a_graceful_no_op():
    dm = UpdateCharacterDM({"cast_spell": "abracadabra"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast a made-up spell"},
    ))

    assert session.characters[player_id].spell_slots == {"1": 2}
    assert dm.tool_result is not None and "no known spell" in dm.tool_result


async def test_cast_spell_not_in_known_spells_is_a_graceful_no_op():
    # cure_wounds is a real spell, just not one a wizard actually knows.
    dm = UpdateCharacterDM({"cast_spell": "cure_wounds"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to cast cure wounds"},
    ))

    assert session.characters[player_id].spell_slots == {"1": 2}
    assert dm.tool_result is not None and "doesn't know" in dm.tool_result


async def test_cast_spell_with_no_slots_remaining_is_a_graceful_no_op():
    dm = UpdateSequenceDM([{"cast_spell": "Magic Missile"}, {"cast_spell": "Shield"}, {"cast_spell": "Mage Armor"}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")  # only 2 level-1 slots

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast three spells in a row"},
    ))

    assert session.characters[player_id].spell_slots == {"1": 0}
    assert "no level 1 spell slots remaining" in dm.tool_results[-1]


async def test_cast_spell_only_applies_to_self_never_an_npc():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": 0, "cast_spell": "Magic Missile"}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "A goblin appears"},
    ))

    assert session.characters[player_id].spell_slots == {"1": 2}  # untouched - cast_spell ignored for an NPC target


async def test_level_up_grows_spell_slots_by_the_real_delta():
    dm = UpdateSequenceDM([{"target": "boss", "max_hp": 999, "hp_delta": -999, "xp": 300}])  # level-2 threshold
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")
    assert session.characters[player_id].spell_slots == {"1": 2}

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the boss down"},
    ))

    character = session.characters[player_id]
    assert character.level == 2
    # Level 2's real max is 3 - grown by the delta (1), not reset to 3
    # outright, so an already-spent slot would stay spent through a
    # level-up (not exercised here, but the addition-not-reset logic is
    # what this confirms).
    assert character.spell_slots == {"1": 3}
    assert character.max_spell_slots == {"1": 3}


async def test_request_roll_spell_resolves_damage_ability_and_proficiency():
    dm = RequestRollDM({"dice": "1d20", "spell": "fire_bolt"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    with patch("server.dice.random.randint", return_value=10):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I cast fire bolt at the rat"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["spell"] == "Fire Bolt"
    assert payload["dice"] == "1d10"
    assert payload["damage_type"] == "fire"
    assert payload["ability"] == "int"  # wizard's real spellcasting ability, auto-resolved
    assert payload["proficiency_bonus"] == 2
    assert payload["roll_kind"] == "attack"  # auto-defaulted


async def test_request_roll_spell_resolves_spiritual_weapon_as_an_attack():
    # spiritual_weapon (added alongside healing_word for cleric) is
    # cleric's own attack-shaped spell, the same real-5e shape fire_bolt
    # already exercises for wizard - locks that the newly-added spell
    # integrates with the existing spell-attack resolution, not just that
    # it loads from srd.json.
    dm = RequestRollDM({"dice": "1d20", "spell": "spiritual_weapon"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "cleric", name="Fenwick")

    with patch("server.dice.random.randint", return_value=10):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I summon my spiritual weapon and strike"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["spell"] == "Spiritual Weapon"
    assert payload["dice"] == "1d8"
    assert payload["damage_type"] == "force"
    assert payload["ability"] == "wis"  # cleric's real spellcasting ability
    assert payload["roll_kind"] == "attack"


async def test_request_roll_spell_resolves_inflict_wounds_as_an_attack():
    # inflict_wounds (2026-08-21 SRD expansion, cleric) mirrors
    # spiritual_weapon's own attack-based shape - real dice/damage/
    # ability resolution, not just that the data loads.
    dm = RequestRollDM({"dice": "1d20", "spell": "inflict_wounds"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "cleric", name="Fenwick")

    with patch("server.dice.random.randint", return_value=10):
        await engine.handle(Envelope(
            type="player_action", session_id="test-session", sender_id=player_id,
            payload={"text": "I channel necrotic power into my touch"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["spell"] == "Inflict Wounds"
    assert payload["dice"] == "3d10"
    assert payload["damage_type"] == "necrotic"
    assert payload["ability"] == "wis"
    assert payload["roll_kind"] == "attack"


async def test_request_roll_spell_non_attack_spell_is_a_graceful_no_op():
    # bless has no "attack": true / "damage" in srd.json - nothing for
    # request_roll to resolve, so the roll falls through to plain 1d20.
    dm = RequestRollDM({"dice": "1d20", "spell": "bless"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "cleric", name="Fenwick")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast bless"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "spell" not in payload
    assert payload["dice"] == "1d20"


async def test_request_roll_spell_explicit_ability_overrides_the_default():
    dm = RequestRollDM({"dice": "1d20", "spell": "fire_bolt", "ability": "dex"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "wizard", name="Gandalf")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I cast fire bolt with a flourish"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[-1][2]["ability"] == "dex"  # explicit override, not INT


async def test_player_initiated_roll_also_applies_disadvantage():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].conditions.append("prone")

    with patch("server.dice.random.randint", side_effect=[18, 4]):
        await engine.handle(Envelope(
            type="dice_roll", session_id="test-session", sender_id=player_id,
            payload={"dice": "1d20", "reason": "perception check"},
        ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["disadvantage_reasons"] == ["prone"]
    assert payload["result"] == 4


async def test_dm_requested_roll_with_unknown_ability_key_has_no_modifier_applied():
    # A blank/unrecognized class has no stats at all - an ability key that
    # doesn't exist on the sheet should be a graceful no-op (no modifier
    # applied, "ability" omitted from the broadcast payload entirely),
    # not a crash or a silent +0 that pretends to be a real DEX check.
    dm = RequestRollDM({"dice": "1d20", "ability": "dex"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)  # no character_class - blank sheet, no stats

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try to dodge"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert "ability" not in payload
    assert "ability_modifier" not in payload
    assert payload["result"] == sum(payload["rolls"])


async def test_npc_introduction_populates_stats_from_a_matched_srd_monster():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -1}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I size up the goblin"},
    ))

    # server/rules/srd.json's real goblin stat block.
    assert session.npcs["goblin"].stats == {"str": 8, "dex": 14, "con": 10, "int": 10, "wis": 8, "cha": 8}


async def test_npc_introduction_leaves_stats_empty_for_an_unmatched_name():
    dm = UpdateSequenceDM([{"target": "shadow_beast", "max_hp": 5, "hp_delta": -1}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I size up the shadow beast"},
    ))

    assert session.npcs["shadow_beast"].stats == {}


def test_compute_ac_unarmored_is_ten_plus_dex_modifier():
    rules = RulesIndex.load_default()
    assert _compute_ac(None, dex_modifier=2, rules=rules) == 12
    assert _compute_ac("Potion of Healing", dex_modifier=1, rules=rules) == 11  # not real armor


def test_compute_ac_uses_the_equipped_armors_base_value():
    rules = RulesIndex.load_default()
    assert _compute_ac("Leather Armor", dex_modifier=1, rules=rules) == 12  # 11 + 1
    # Case/whitespace shouldn't matter - the same slug()-based lookup
    # every other equipment/monster name match in this project already uses.
    assert _compute_ac("leather armor", dex_modifier=0, rules=rules) == 11


def test_compute_ac_only_counts_the_equipped_armor_not_everything_carried():
    # A real behavior change from _compute_ac's original "any armor
    # anywhere in inventory counts" shape - carrying a spare suit of
    # armor you haven't equipped shouldn't silently raise your AC.
    rules = RulesIndex.load_default()
    assert _compute_ac(None, dex_modifier=1, rules=rules) == 11  # unarmored, even with armor "around"


def test_compute_ac_medium_armor_caps_a_positive_dex_modifier():
    # Real 5e RAW: medium armor's Dex bonus is capped at a real max (2
    # here, "14 + Dex modifier (max 2)") - closes the "untested future
    # work" gap _compute_ac's own docstring named before srd.json's
    # equipment table grew beyond a single light-armor entry.
    rules = RulesIndex.load_default()
    assert _compute_ac("Breastplate", dex_modifier=4, rules=rules) == 16  # 14 + 2, not 14 + 4
    assert _compute_ac("Breastplate", dex_modifier=1, rules=rules) == 15  # under the cap, applies in full


def test_compute_ac_medium_armor_does_not_cap_a_negative_dex_modifier():
    # Real 5e RAW: the cap only limits how much Dex can *help* - a
    # negative modifier still applies in full, it isn't floored at the
    # same cap value.
    rules = RulesIndex.load_default()
    assert _compute_ac("Breastplate", dex_modifier=-2, rules=rules) == 12  # 14 - 2, not clamped


def test_compute_ac_heavy_armor_ignores_dex_modifier_entirely():
    # Real 5e RAW: heavy armor contributes zero Dex, positive or negative -
    # a flat base AC regardless of the wearer's own Dex score.
    rules = RulesIndex.load_default()
    assert _compute_ac("Plate Armor", dex_modifier=3, rules=rules) == 18
    assert _compute_ac("Plate Armor", dex_modifier=-3, rules=rules) == 18  # not 15 - the min(dex, 0) bug this guards against


def test_compute_ac_adds_a_shields_bonus_additively():
    # A real second equipment slot (server/state.py's equipped_shield),
    # not a replacement value the way equipped_armor's own `ac` field is -
    # closes the gap ATTRIBUTION.md's own equipment-coverage note flagged.
    rules = RulesIndex.load_default()
    assert _compute_ac(None, dex_modifier=1, rules=rules, equipped_shield="Shield") == 13  # 10 + 1 + 2
    assert _compute_ac("Leather Armor", dex_modifier=1, rules=rules, equipped_shield="Shield") == 14  # 11 + 1 + 2
    assert _compute_ac("Plate Armor", dex_modifier=-3, rules=rules, equipped_shield="Shield") == 20  # 18 + 0 + 2


def test_compute_ac_adds_armor_and_shield_magic_bonuses_additively():
    # A magic item's real InventoryItem.magic_bonus (structured items,
    # server/state.py) stacks on top of the SRD base stats the same way a
    # shield's own ac_bonus already does - a +1 suit of armor is still
    # whatever armor it is, plus 1.
    rules = RulesIndex.load_default()
    assert _compute_ac(
        "Leather Armor", dex_modifier=1, rules=rules, armor_magic_bonus=1
    ) == 13  # 11 + 1 + 1
    assert _compute_ac(
        None, dex_modifier=1, rules=rules, equipped_shield="Shield", shield_magic_bonus=1
    ) == 14  # 10 + 1 + 2 + 1
    assert _compute_ac(
        "Leather Armor", dex_modifier=1, rules=rules,
        equipped_shield="Shield", armor_magic_bonus=1, shield_magic_bonus=2,
    ) == 17  # 11 + 1 + 1 + 2 + 2


def test_compute_ac_unrecognized_shield_contributes_nothing():
    rules = RulesIndex.load_default()
    assert _compute_ac(None, dex_modifier=1, rules=rules, equipped_shield="not a real item") == 11


def test_equipment_dataset_is_internally_consistent_at_scale():
    # A real sanity check for the full-SRD-equipment expansion (132 new
    # entries in one pass, ROADMAP.md 2026-08-10) - not testing any one
    # item, but that hand-authoring that many entries didn't introduce a
    # duplicate key/name or a malformed required field somewhere in the
    # noise. get_entry's own slug() lookup (case/whitespace-insensitive,
    # server/rules) is exercised here too, across every real entry, not
    # just the handful individually spot-checked elsewhere.
    rules = RulesIndex.load_default()
    equipment = rules.all_entries("equipment")
    assert len(equipment) >= 100  # weapons + armor + gear + packs + tools + trade goods + magic items

    seen_names = set()
    for key, entry in equipment.items():
        assert entry.get("name"), f"{key} has no name"
        assert entry["name"] not in seen_names, f"duplicate display name: {entry['name']}"
        seen_names.add(entry["name"])
        # Every entry is either a weapon (damage), armor (ac), or general
        # gear/tool/trade-good/magic-item (weight or rarity) - nothing
        # with none of those, which would mean a copy-paste field typo.
        assert entry.get("damage") or entry.get("ac") or "weight" in entry or entry.get("rarity"), key
        # Real end-to-end lookup, not just presence in the raw dict -
        # confirms get_entry's slug() normalization round-trips correctly
        # for every single new key, not a hand-picked few.
        assert rules.get_entry("equipment", entry["name"]) == entry


def test_full_weapon_and_armor_tables_are_present():
    # A representative spot check across every real SRD weapon/armor
    # category this expansion added, not just the pre-existing 8 items -
    # martial/simple, melee/ranged weapons, and all three armor weight
    # classes.
    rules = RulesIndex.load_default()
    for name in (
        "Greatsword", "Longbow", "Dagger", "Sling",  # martial melee, martial ranged, simple melee, simple ranged
        "Padded Armor", "Breastplate", "Plate Armor",  # light, medium, heavy
    ):
        entry = rules.get_entry("equipment", name)
        assert entry is not None, f"missing: {name}"
        assert entry["name"] == name


async def test_npc_introduction_copies_ac_from_a_matched_srd_monster():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": -1}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I size up the goblin"},
    ))

    assert session.npcs["goblin"].ac == 15  # server/rules/srd.json's real goblin ac


async def test_npc_introduction_leaves_ac_at_default_for_an_unmatched_name():
    dm = UpdateSequenceDM([{"target": "shadow_beast", "max_hp": 5, "hp_delta": -1}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I size up the shadow beast"},
    ))

    assert session.npcs["shadow_beast"].ac == 10  # CharacterSheet's own unarmored default


async def test_public_character_view_includes_ac():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))

    other_id = str(uuid.uuid4())
    await join(engine, other_id, name="Rowan")

    syncs = [r for r in received if r[0] == "send_to" and r[1] == other_id and r[2] == "state_sync"]
    view = syncs[-1][3]["characters"][player_id]
    assert view["ac"] == 12  # fighter's real starting AC (leather armor 11 + DEX 13 -> +1)


async def test_dm_requested_weapon_damage_roll_uses_real_srd_damage_die():
    dm = RequestRollDM({"weapon": "longsword", "ability": "str", "reason": "damage roll"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I swing my longsword"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["dice"] == "1d8"  # server/rules/srd.json's real longsword damage die
    assert payload["damage_type"] == "slashing"
    assert payload["ability"] == "str"
    assert payload["ability_modifier"] == 2  # fighter's real STR modifier
    assert 1 <= (payload["result"] - 2) <= 8  # the raw d8 roll, before the +2 STR mod

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "1d8 (slashing) +2 STR" in dice_logs[-1][2]["text"]
    assert dm.tool_result is not None and "(slashing)" in dm.tool_result


async def test_dm_requested_weapon_damage_roll_adds_a_real_carried_magic_bonus():
    # The structured-items feature: a weapon's real magic_bonus (granted
    # via the DM's own add_item + magic_bonus, server/narrator.py's
    # UPDATE_CHARACTER_TOOL) adds to its damage roll automatically. No
    # starting class (a blank/unrecognized class starts with no inventory
    # at all) - a fighter's own starting Longsword would otherwise be a
    # second, mundane stack find_item's "first match" could resolve to
    # instead of the magic one this test actually adds.
    dm = GrantsItemThenRollsWeaponDM(
        {"add_item": "Longsword", "magic_bonus": 1},
        {"weapon": "longsword", "reason": "damage roll"},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I swing my new +1 longsword"},
    ))

    character = session.characters[player_id]
    assert character.find_item("Longsword").magic_bonus == 1

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["weapon_magic_bonus"] == 1
    assert 1 + 1 <= payload["result"] <= 8 + 1  # the raw d8 roll + 1 magic

    dice_logs = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "log_entry" and r[2].get("kind") == "dice"
    ]
    assert "+1 magic" in dice_logs[-1][2]["text"]


async def test_dm_requested_weapon_damage_roll_omits_magic_bonus_for_a_mundane_weapon():
    dm = RequestRollDM({"weapon": "longsword", "ability": "str", "reason": "damage roll"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain", "character_class": "fighter"},
    ))

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I swing my longsword"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert "weapon_magic_bonus" not in results[-1][2]


async def test_dm_requested_roll_with_unmatched_weapon_falls_back_to_given_dice():
    dm = RequestRollDM({"dice": "2d6", "weapon": "not-a-real-weapon", "reason": "improvised damage"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I improvise a weapon"},
    ))

    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    payload = results[-1][2]
    assert payload["dice"] == "2d6"
    assert "damage_type" not in payload


async def test_dm_requested_roll_with_invalid_notation_reports_error_without_crashing_turn():
    dm = RequestRollDM({"dice": "not-dice"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I try something"},
    ))

    assert dm.tool_result is not None and "Invalid dice notation" in dm.tool_result
    assert not any(r[0] == "broadcast" and r[1] == "dice_result" for r in received)
    narration_chunks = [r for r in received if r[0] == "broadcast" and r[2].get("kind") == "narration"]
    assert narration_chunks, "the turn should still narrate and complete normally"


async def test_narrator_failure_notifies_player_and_keeps_their_turn():
    """Regression test: a narrator exception used to crash the whole connection
    (see docs/protocol.md history / README) instead of surfacing an error."""
    engine, session, received = make_engine(FailingDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))

    errors = [r for r in received if r[0] == "send_to" and r[3].get("level") == "error"]
    assert errors, "narrator failure should notify the player"
    assert session.current_turn == player_id, "turn should not advance on failure"


async def test_update_world_tool_call_broadcasts_world_update():
    dm = UpdateWorldDM({"add_objective": "Find the missing merchant", "location": "Market Square"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I ask around town"},
    ))

    assert session.world.location == "Market Square"
    assert [o.text for o in session.world.objectives] == ["Find the missing merchant"]

    updates = [r for r in received if r[0] == "broadcast" and r[1] == "world_update"]
    assert updates, "a real world-state change should broadcast world_update"
    payload = updates[-1][2]
    assert payload["location"] == "Market Square"
    assert payload["objectives"] == [{"text": "Find the missing merchant", "status": "active"}]


async def test_update_world_can_expire_or_fail_an_objective():
    engine, session, received = make_engine(UpdateWorldDM({"expire_objective": "Meet the caravan before dawn"}))
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.world.apply_update({"add_objective": "Meet the caravan before dawn"})

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I wait at the crossroads"},
    ))

    assert session.world.objectives[0].status == "expired"
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "world_update"]
    assert updates[-1][2]["objectives"] == [{"text": "Meet the caravan before dawn", "status": "expired"}]


async def test_update_world_connect_locations_broadcasts_the_map():
    # ROADMAP.md item 8 - the map/visual panel design pass. A real
    # end-to-end check that connect_locations reaches the same
    # world_update broadcast every other WorldState field already uses,
    # not just a state.py-level unit test of apply_update itself.
    dm = UpdateWorldDM({"connect_locations": ["Great Hall", "Armory"]})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I push through the door into the next room"},
    ))

    assert session.world.location_map == {"Great Hall": ["Armory"], "Armory": ["Great Hall"]}
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "world_update"]
    assert updates[-1][2]["location_map"] == {"Great Hall": ["Armory"], "Armory": ["Great Hall"]}


async def test_update_world_mood_broadcasts():
    dm = UpdateWorldDM({"mood": "tense"})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I descend into the sunless crypt"},
    ))

    assert session.world.mood == "tense"
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "world_update"]
    assert updates[-1][2]["mood"] == "tense"


async def test_update_world_no_op_does_not_broadcast():
    dm = UpdateWorldDM({})
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert not any(r[0] == "broadcast" and r[1] == "world_update" for r in received)


async def start_session(engine, player_id, content_preference=None):
    payload = {"content_preference": content_preference} if content_preference else {}
    await engine.handle(Envelope(
        type="start_session", session_id="test-session", sender_id=player_id, payload=payload,
    ))


async def test_start_session_sets_a_recognized_content_preference():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await start_session(engine, player_id, content_preference="intense")

    assert session.content_preference == "intense"


async def test_start_session_ignores_an_unrecognized_content_preference():
    # The same "graceful-miss convention every other name-based field in
    # this file already follows" - a malformed/adversarial payload value
    # falls back to the field's own default rather than raising.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await start_session(engine, player_id, content_preference="chaotic-evil")

    assert session.content_preference == "standard"


async def test_content_preference_hint_is_prepended_to_turns_when_non_standard():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id, content_preference="lighter")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert len(dm.action_texts) == 1
    assert "lighter" in dm.action_texts[0]
    assert dm.action_texts[0].endswith("I look around")
    # The hint is invisible to players - the raw text broadcast to the
    # visible action log comes from the envelope directly, not from the
    # (possibly hint-prefixed) action_text narrate() actually receives.
    action_lines = [
        r[2]["text"] for r in received
        if r[0] == "broadcast" and r[2].get("kind") == "action"
    ]
    assert action_lines == [f"Thrain: I look around"]


async def test_content_preference_hint_is_absent_for_standard_tone():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert dm.action_texts == ["I look around"]


async def test_join_never_narrates_regardless_of_opening_scene_flag():
    # Regression guard for the test helper itself: most existing tests in
    # this file rely on join() having no narration side effect - true now
    # unconditionally, since narration only ever fires via an explicit
    # start_session (see the next test), never automatically on join.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    assert dm.action_texts == []
    assert not any(r for r in received if r[0] == "broadcast" and r[1] == "turn_prompt"), \
        "turn_prompt shouldn't show before the adventure has started"


async def test_opening_scene_fires_on_explicit_start_not_on_join():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await start_session(engine, player_id)

    assert len(dm.action_texts) == 1
    assert "adventure begins" in dm.action_texts[0]
    narration_chunks = [r for r in received if r[0] == "broadcast" and r[2].get("kind") == "narration"]
    assert narration_chunks, "the opening scene should stream narration like a real turn"
    assert session.log[-1]["text"] == "Scene."
    assert session.current_turn == player_id  # doesn't consume the player's real first turn
    started = [r for r in received if r[0] == "broadcast" and r[1] == "session_started"]
    assert started, "start_session should broadcast session_started once the adventure begins"
    turn_prompts = [r for r in received if r[0] == "broadcast" and r[1] == "turn_prompt"]
    assert turn_prompts, "turn_prompt should now be visible once the adventure has started"


async def test_narrate_receives_the_current_world_state_as_a_summary():
    # Confirms the wiring itself works end to end - this data actually
    # reaching the DM was built to test a real hypothesis for
    # complete_objective's own 0% measured reliability (ROADMAP.md's
    # update_world investigation): that recalling an objective's exact
    # text from several turns back was the bottleneck. Real --repeat
    # testing found that wasn't the case - see WORLD_UPDATE_PROMPT_ADDENDUM
    # (server/narrator_ollama.py) for the full writeup. Kept as real,
    # defensible context for the DM regardless of that specific result.
    dm = OpeningSceneDM()
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    # A fresh session has nothing to report yet.
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))
    assert dm.world_summaries[-1] == ""

    session.world.apply_update({"location": "Millbrook", "add_objective": "Find the missing goat"})

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I ask around town"},
    ))
    assert dm.world_summaries[-1] == "Current location: Millbrook\nActive objectives:\n- Find the missing goat"


async def test_narrate_world_summary_includes_the_tracked_npc_roster():
    # _npc_roster rides world_summary so dispositions/notes/wounds stay
    # visible to the DM past the rolling history window.
    dm = UpdateSequenceDM(
        [{"target": "bandit", "max_hp": 10, "hp_delta": -7,
          "notes": "A greedy toll-keeper.", "disposition": "hostile"}]
    )
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the bandit"},
    ))

    session.world.apply_update({"location": "Millbrook"})
    engine._dm = recorder = OpeningSceneDM()
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    summary = recorder.world_summaries[-1]
    assert summary.startswith("Current location: Millbrook\nTracked NPCs:")
    assert "- bandit: HP 3/10, hostile - A greedy toll-keeper." in summary


async def test_tracked_npc_roster_excludes_the_dead_and_omits_neutral_disposition():
    dm = UpdateSequenceDM([
        {"target": "bandit", "max_hp": 6, "hp_delta": -6},
        {"target": "guard", "max_hp": 8, "hp_delta": -2, "add_condition": "poisoned"},
        {"target": "innkeeper", "disposition": "friendly",
         "notes": "w" * 100},
    ])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "Chaos erupts in the tavern"},
    ))

    engine._dm = recorder = OpeningSceneDM()
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    summary = recorder.world_summaries[-1]
    assert "bandit" not in summary, "a dead NPC has left active play"
    assert "- guard: HP 6/8, poisoned" in summary
    assert "- innkeeper: HP 10/10, friendly" in summary
    assert "w" * 100 not in summary, "notes are truncated in the roster"


async def test_opening_scene_prompt_is_grounded_in_the_world_bible():
    # The near-death/transport/Guardian-greeting premise is composed from
    # real setting data (server/lore), not left for the DM to invent (and
    # potentially contradict on a later turn) - confirms the engine's
    # default WorldBible actually reaches the opening scene's action_text.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")

    await start_session(engine, player_id)

    prompt = dm.action_texts[0]
    assert "Ashwren" in prompt  # the default world bible's Guardian
    assert "Aetherfall" in prompt  # the default world bible's setting name
    assert "nearly died" in prompt


async def test_opening_scene_prompt_includes_the_solo_players_own_origin():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())
    await join(engine, player_id, name="Thrain")

    await start_session(engine, player_id)

    prompt = dm.action_texts[0]
    background = session.characters[player_id].background
    assert background  # the join above should have generated a real one
    assert background in prompt


async def test_opening_scene_prompt_omits_origin_for_a_multiplayer_start():
    # No single character's origin should be singled out over the others'
    # when several players are present - see WorldBible.opening_scene_prompt.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    await join(engine, first_id, name="Thrain")
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=second_id,
        payload={"player_name": "Rowan", "character_class": "rogue"},
    ))

    await start_session(engine, first_id)

    prompt = dm.action_texts[0]
    assert "specific background" not in prompt


async def test_start_session_uses_a_custom_world_bible_when_given():
    dm = OpeningSceneDM()
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env: Envelope):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env: Envelope):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(
        session, dm, broadcast, send_to, enable_opening_scene=True, world_bible=make_world_bible()
    )
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id, payload={"player_name": "Thrain"},
    ))

    await engine.handle(Envelope(type="start_session", session_id="test-session", sender_id=player_id, payload={}))

    prompt = dm.action_texts[0]
    assert "Testwarden" in prompt
    assert "Testonia" in prompt
    assert "Ashwren" not in prompt  # the default world bible's Guardian, not this custom one


async def test_start_session_with_multiple_players_mentions_everyone_in_the_prompt():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    await join(engine, first_id, name="Thrain")
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=second_id,
        payload={"player_name": "Rowan", "character_class": "rogue"},
    ))

    await start_session(engine, first_id)

    assert len(dm.action_texts) == 1
    prompt = dm.action_texts[0]
    assert "Thrain" in prompt
    assert "Rowan the Rogue" in prompt
    assert "introduce themselves" in prompt


async def test_start_session_is_idempotent_once_the_adventure_has_begun():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    assert len(dm.action_texts) == 1

    await start_session(engine, player_id)  # e.g. a second player also clicking Start

    assert len(dm.action_texts) == 1, "a second start_session shouldn't re-narrate the opening scene"
    started = [r for r in received if r[0] == "broadcast" and r[1] == "session_started"]
    assert len(started) == 1, "session_started shouldn't rebroadcast either"


async def test_start_session_with_no_players_is_a_defensive_no_op():
    engine, session, received = make_engine(StubDM(), enable_opening_scene=True)
    player_id = str(uuid.uuid4())  # never actually joined

    await start_session(engine, player_id)

    assert session.log == []
    assert not any(r for r in received if r[0] == "broadcast" and r[1] == "session_started")


async def test_opening_scene_disabled_does_not_narrate_but_session_still_starts():
    # enable_opening_scene=False (make_engine()'s own default, and the
    # reliability harness's - scripts/live_reliability_check.py) should
    # suppress narration but still let the lobby transition happen and
    # turn_prompt become visible.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await start_session(engine, player_id)

    assert dm.action_texts == []
    started = [r for r in received if r[0] == "broadcast" and r[1] == "session_started"]
    assert started, "the lobby should still transition even with narration disabled"
    assert any(r for r in received if r[0] == "broadcast" and r[1] == "turn_prompt")


async def test_opening_scene_failure_warns_but_session_still_starts():
    engine, session, received = make_engine(FailingDM(), enable_opening_scene=True)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await start_session(engine, player_id)

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings, "a failed opening scene should warn, not crash the start"
    assert session.current_turn == player_id, "the player should still be seated normally"
    assert session.log == [], "a failed opening scene shouldn't leave a partial log entry"
    started = [r for r in received if r[0] == "broadcast" and r[1] == "session_started"]
    assert started, "the session should still transition out of the lobby even if narration failed"


async def _join_as(engine, player_id, character_class, name="Thrain"):
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": name, "character_class": character_class},
    ))


async def _start_combat(engine, player_id):
    await engine.handle(Envelope(type="start_combat", session_id="test-session", sender_id=player_id, payload={}))


async def _end_combat(engine, player_id):
    await engine.handle(Envelope(type="end_combat", session_id="test-session", sender_id=player_id, payload={}))


async def test_start_combat_orders_players_by_initiative_roll():
    # Fighter (DEX 13, +1 mod) and rogue (DEX 15, +2 mod) - rolls chosen so
    # the rogue's lower raw roll still wins once its higher DEX modifier is
    # added, confirming the modifier is genuinely applied, not just the
    # bare d20.
    engine, session, received = make_engine(StubDM())
    fighter_id, rogue_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _join_as(engine, fighter_id, "fighter", name="Thrain")
    await _join_as(engine, rogue_id, "rogue", name="Rowan")

    with patch("server.dice.random.randint", side_effect=[10, 9]):  # fighter=11, rogue=11... see below
        await _start_combat(engine, fighter_id)

    # fighter: 10 + 1 = 11; rogue: 9 + 2 = 11 - a genuine tie, broken by the
    # higher DEX modifier (rogue's +2 beats fighter's +1), confirming the
    # tiebreak itself works, not just the common no-tie case.
    assert session.turn_order == [rogue_id, fighter_id]
    assert session.in_combat is True

    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert announcements


async def test_start_combat_announcement_names_everyone_with_their_roll():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter", name="Thrain")

    with patch("server.dice.random.randint", return_value=15):  # 15 + 1 (fighter's DEX mod) = 16
        await _start_combat(engine, player_id)

    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert announcements
    assert "Thrain (16)" in announcements[0][2]["text"]


async def test_start_combat_announces_npcs_but_excludes_them_from_turn_order():
    dm = UpdateSequenceDM([{"target": "goblin", "max_hp": 7, "hp_delta": 0}])  # introduces, no damage
    # npc.name preserves the DM's own first-seen casing (documented elsewhere
    # in this file) - "goblin" here, not "Goblin".
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter", name="Thrain")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "A goblin appears"},
    ))
    assert "goblin" in session.npcs

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player_id)

    # Only the player id is in the mechanical turn_order - NPCs are named
    # in the announcement for narrative/pacing clarity but never occupy a
    # real turn slot (see _on_start_combat's own docstring for why).
    assert session.turn_order == [player_id]
    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert "goblin" in announcements[0][2]["text"]


async def test_start_combat_initiative_excludes_a_mistargeted_condition_name():
    # Closes the exact live-found bug this fix targets end to end, not just
    # at the apply_update layer above: before the fix, a mistargeted hit on
    # the acting character's own "Veil-Touched" condition spawned a phantom
    # NPC, which /combat start's own "roll initiative for every tracked NPC"
    # logic then swept into the announcement alongside the real player -
    # a bogus participant with no real presence in the scene.
    dm = UpdateSequenceDM([
        {"target": "self", "add_condition": "Veil-Touched"},
        {"target": "Veil-Touched", "hp_delta": -2},
    ])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter", name="Thrain")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "Something lunges out of the shadows"},
    ))
    assert session.npcs == {}

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player_id)

    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert "Veil-Touched" not in announcements[0][2]["text"]


async def test_start_combat_dex_modifier_fallback_for_npc_without_stats():
    # "cutpurse" matches no known SRD monster (unlike "bandit", a real SRD
    # entry - server/rules/srd.json, ROADMAP.md item 11), so it never gets
    # real stats - the same fallback every other stat-dependent mechanic
    # here uses.
    dm = UpdateSequenceDM([{"target": "cutpurse", "max_hp": 10, "hp_delta": 0}])
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter", name="Thrain")

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "A cutpurse appears"},
    ))
    assert session.npcs["cutpurse"].stats == {}

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player_id)  # would raise/KeyError if the fallback were missing

    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert "cutpurse (10)" in announcements[0][2]["text"]  # +0 modifier, bare roll


async def test_start_combat_is_idempotent():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter")

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player_id)
    order_after_first = list(session.turn_order)
    received.clear()

    await _start_combat(engine, player_id)  # no dice.roll patch active - would error if it actually rolled again

    assert session.turn_order == order_after_first
    assert not any(r for r in received if r[0] == "broadcast" and r[1] == "system_message")


async def test_start_combat_with_no_players_is_a_defensive_no_op():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())  # never actually joined

    await _start_combat(engine, player_id)

    assert session.in_combat is False
    assert session.turn_order == []


async def test_start_combat_is_exempt_from_turn_order():
    engine, session, received = make_engine(StubDM())
    player1_id, player2_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _join_as(engine, player1_id, "fighter", name="Thrain")
    await _join_as(engine, player2_id, "rogue", name="Rowan")
    assert session.current_turn != player2_id

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player2_id)  # not player2's turn, but start_combat isn't turn-gated

    assert session.in_combat is True


async def test_end_combat_restores_pre_combat_order():
    engine, session, received = make_engine(StubDM())
    player1_id, player2_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _join_as(engine, player1_id, "fighter", name="Thrain")
    await _join_as(engine, player2_id, "rogue", name="Rowan")
    join_order = list(session.turn_order)

    with patch("server.dice.random.randint", side_effect=[5, 18]):  # rogue (18+2=20) beats fighter (5+1=6)
        await _start_combat(engine, player1_id)
    assert session.turn_order != join_order

    received.clear()
    await _end_combat(engine, player1_id)

    assert session.turn_order == join_order
    assert session.in_combat is False
    assert session.pre_combat_turn_order is None
    ends = [r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Combat ends" in r[2]["text"]]
    assert ends


async def test_end_combat_appends_players_who_joined_mid_combat():
    engine, session, received = make_engine(StubDM())
    player1_id = str(uuid.uuid4())
    await _join_as(engine, player1_id, "fighter", name="Thrain")

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player1_id)

    player2_id = str(uuid.uuid4())
    await _join_as(engine, player2_id, "rogue", name="Rowan")  # joins mid-combat
    assert session.turn_order == [player1_id, player2_id]  # appended live, same as always

    await _end_combat(engine, player1_id)

    # Restored pre-combat order (just player1) with the mid-combat joiner
    # appended after, not lost and not re-sorted by initiative.
    assert session.turn_order == [player1_id, player2_id]


async def test_end_combat_is_idempotent():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await _join_as(engine, player_id, "fighter")
    received.clear()  # drop the "joined the session" broadcast

    await _end_combat(engine, player_id)  # never in combat at all

    assert not any(r for r in received if r[0] == "broadcast" and r[1] == "system_message")


async def test_advance_turn_cycles_through_initiative_order():
    engine, session, received = make_engine(StubDM())
    player1_id, player2_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _join_as(engine, player1_id, "fighter", name="Thrain")
    await _join_as(engine, player2_id, "rogue", name="Rowan")

    with patch("server.dice.random.randint", side_effect=[5, 18]):  # rogue goes first
        await _start_combat(engine, player1_id)
    assert session.current_turn == player2_id

    session.advance_turn()
    assert session.current_turn == player1_id

    session.advance_turn()
    assert session.current_turn == player2_id  # cycles back to the top of the round


async def test_rejoin_after_session_started_shows_turn_prompt():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)

    received.clear()
    await join(engine, player_id)  # e.g. the client restarted mid-game

    assert any(r for r in received if r[0] == "broadcast" and r[1] == "turn_prompt"), \
        "reconnecting into an already-started game should still show whose turn it is"


def _recap_texts(received: list[tuple], player_id: str) -> list[str]:
    return [
        r[3]["text"] for r in received
        if r[0] == "send_to" and r[1] == player_id and r[2] == "system_message" and r[3]["text"].startswith("Welcome back.")
    ]


async def test_no_recap_before_the_session_has_started():
    # A reconnect during the pre-game lobby (nothing has happened yet) has
    # no story to recap - only the existing name-mismatch notice, if any,
    # should be private send_to traffic here.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await join(engine, player_id)  # reconnect, still pre-game

    assert _recap_texts(received, player_id) == []


async def test_reconnect_into_a_started_session_gets_a_private_recap():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    received.clear()

    await join(engine, player_id)  # e.g. the client restarted mid-game

    recaps = _recap_texts(received, player_id)
    assert len(recaps) == 1
    # No other player should ever see this - it's about what *this*
    # player should know about their own reconnect, not table news.
    assert not any(r for r in received if r[0] == "broadcast" and r[2].get("text", "").startswith("Welcome back."))


async def test_a_brand_new_player_joining_an_in_progress_session_also_gets_a_recap():
    # Arguably needs it even more than a returning player - joining a
    # multiplayer game already underway with zero context otherwise.
    engine, session, received = make_engine(StubDM())
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    await join(engine, first_id, name="Thrain")
    await start_session(engine, first_id)
    received.clear()

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=second_id,
        payload={"player_name": "Rowan"},
    ))

    assert len(_recap_texts(received, second_id)) == 1


async def test_recap_uses_the_world_summary_when_present():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    session.world.summary = "The party has allied with the Warden against a growing threat in the Sunken Vale."
    received.clear()

    await join(engine, player_id)

    recap = _recap_texts(received, player_id)[0]
    assert "The party has allied with the Warden against a growing threat in the Sunken Vale." in recap


async def test_recap_falls_back_to_last_narration_when_no_summary_is_set():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the goblin"},
    ))
    assert session.world.summary == ""  # nothing ever called update_world here
    received.clear()

    await join(engine, player_id)

    recap = _recap_texts(received, player_id)[0]
    assert "Last thing that happened:" in recap
    # StubDM's own narration text, see its narrate() above.
    assert "swing your sword" in recap


async def test_recap_includes_location_and_only_active_objectives():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    session.world.location = "Emberreach"
    session.world.objectives = [
        Objective(text="Find the missing heirloom", status="active"),
        Objective(text="Escape the Hollow March", status="completed"),
    ]
    received.clear()

    await join(engine, player_id)

    recap = _recap_texts(received, player_id)[0]
    assert "You're currently at Emberreach." in recap
    assert "Find the missing heirloom" in recap
    assert "Escape the Hollow March" not in recap


class NarratesFixedTextDM:
    """Narrates the given fixed text and, if update: a dict is provided,
    calls apply_update with it first - simulating a DM turn that may or may
    not actually invoke the tool, independent of what the narration says."""

    def __init__(self, text: str, update: dict | None = None):
        self._text = text
        self._update = update

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        if self._update is not None:
            apply_update(self._update)
        yield self._text


class NarratesThenSelfCorrectsDM(NarratesFixedTextDM):
    """Narrates the given fixed text with no tool call this turn (like
    NarratesFixedTextDM with update=None), but implements
    check_missed_change - simulating a real backend that gets a second
    chance to self-correct and takes it (or, if correction is None,
    reviews and finds nothing to fix). An optional `proposal` (a dict)
    makes it also implement propose_correction, returning that best-guess
    update for the player to confirm via /apply."""

    def __init__(self, text: str, correction: dict | None, proposal: dict | None = None):
        super().__init__(text)
        self._correction = correction
        self._proposal = proposal
        self.check_missed_change_calls: list[tuple] = []
        self.propose_correction_calls: list[tuple] = []

    async def check_missed_change(self, narration, character_summary, apply_update):
        self.check_missed_change_calls.append((narration, character_summary))
        if self._correction is None:
            return False
        apply_update(self._correction)
        return True

    async def propose_correction(self, narration, character_summary):
        self.propose_correction_calls.append((narration, character_summary))
        return self._proposal


def _missed_change_warnings(received: list[tuple], player_id: str) -> list[tuple]:
    return [
        r for r in received
        if r[0] == "send_to" and r[1] == player_id and r[2] == "system_message"
        and "out of sync" in r[3]["text"]
    ]


def _missed_change_corrections(received: list[tuple], player_id: str) -> list[tuple]:
    return [
        r for r in received
        if r[0] == "send_to" and r[1] == player_id and r[2] == "system_message"
        and "double-checked" in r[3]["text"]
    ]


async def test_missed_change_self_correction_applies_and_notifies_instead_of_warning():
    # The backend gets a real second chance (NarratorBackend.check_missed_change,
    # server/narrator.py) rather than the player only ever seeing a passive
    # warning - a real correction here should both update the sheet through
    # the normal apply_update path and replace the warning with a positive
    # confirmation.
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding, and falls dead.",
        correction={"target": "bandit", "max_hp": 7, "hp_delta": -7},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    assert dm.check_missed_change_calls, "the backend should have been given a chance to self-correct"
    assert "bandit" in session.npcs
    assert session.npcs["bandit"].hp == 0

    assert not _missed_change_warnings(received, player_id), (
        "a real correction should replace the passive warning, not sit alongside it"
    )
    assert _missed_change_corrections(received, player_id)

    npc_updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert npc_updates, "the correction should broadcast the same way a real in-turn tool call would"


async def test_missed_change_self_correction_is_not_styled_as_a_warning():
    # advisory=True renders with a yellow warning-triangle (client/app.py)
    # - the right treatment for "you might want to double check", wrong
    # for a real confirmation the sheet's already been fixed.
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding, and falls dead.",
        correction={"target": "bandit", "hp_delta": -6},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    correction = _missed_change_corrections(received, player_id)[0]
    assert not correction[3].get("advisory")
    assert correction[3]["level"] == "info"


async def test_missed_change_self_correction_falls_back_to_warning_when_dm_finds_nothing():
    # check_missed_change reviewed the narration and genuinely found no
    # real change to correct - the original passive warning still applies,
    # the same as if the backend had no self-correction capability at all.
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding, and falls dead.",
        correction=None,
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    assert dm.check_missed_change_calls
    assert _missed_change_warnings(received, player_id)
    assert not _missed_change_corrections(received, player_id)


async def test_missed_change_self_correction_not_attempted_when_a_real_call_already_fired():
    # check_missed_change should only ever be reached via the same gating
    # the passive warning already has (not sheet_changed, not npcs_touched) -
    # a turn that already made a real tool call shouldn't also trigger a
    # redundant self-correction pass.
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding, and falls dead.",
        correction={"target": "bandit", "hp_delta": -1},
    )
    dm._update = {"target": "bandit", "max_hp": 7, "hp_delta": -7}
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    assert not dm.check_missed_change_calls
    assert session.npcs["bandit"].hp == 0  # only the real in-turn call's -7, not also the correction's -1


async def test_missed_change_heuristic_warns_when_damage_language_has_no_tool_call():
    # Reconstructs the exact real failure shape logged in ROADMAP.md's
    # tool-call reliability investigation: narration confirms lethal damage
    # to an NPC, but no update_character call fires at all that turn.
    dm = NarratesFixedTextDM("Your blade finds its mark - the bandit staggers, bleeding, and falls dead.")
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    assert _missed_change_warnings(received, player_id), (
        "narration describing a death with no tool call should warn the player their sheet may be stale"
    )


async def test_missed_change_heuristic_warning_carries_advisory_flag():
    # advisory distinguishes this specific nudge from every other
    # system_message (connection/turn-order/save-failure) so the client can
    # give it a visually distinct treatment instead of rendering it
    # identically to a plain operational warning.
    dm = NarratesFixedTextDM("Your blade finds its mark - the bandit staggers, bleeding, and falls dead.")
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    warnings = _missed_change_warnings(received, player_id)
    assert warnings and warnings[0][3]["advisory"] is True


async def test_out_of_turn_warning_does_not_carry_advisory_flag():
    # A real, different category of warning - an ordinary operational
    # rejection, not the missed-change nudge - should render the same as
    # before (no advisory flag at all, not even advisory: False).
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=other_id,
        payload={"text": "I do nothing"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert "advisory" not in warnings[0][3]


async def test_missed_change_heuristic_silent_when_a_real_tool_call_fired():
    # Same trigger language as above, but this time the DM actually called
    # update_character - the heuristic must not double-warn on a turn that
    # already did the right thing.
    dm = NarratesFixedTextDM(
        "Your blade finds its mark - the bandit staggers, bleeding, and falls dead.",
        update={"target": "bandit", "hp_delta": -10},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    assert not _missed_change_warnings(received, player_id), (
        "a real update_character call this turn means the sheet isn't stale - no warning is warranted"
    )


async def test_missed_change_heuristic_warns_on_condition_language_with_no_tool_call():
    # Reconstructs a real failure live-reproduced 2026-08-07 (see
    # ROADMAP.md's GPU-migration entry): a combat turn narrated a leaked
    # `add_condition: "frozen"` pseudo-tool-call as plain text ("chilling
    # your skin", "numbing cold spreading") with zero real update_character
    # call - and this heuristic stayed silent, since its pattern's stated
    # intent ("damage/death/condition") had no actual condition keywords in
    # it. Guards against that regression now that they've been added.
    dm = NarratesFixedTextDM(
        "The shadow's icy breath washes over you, chilling your skin. You feel a numbing "
        "cold spreading through you, and your limbs grow stiff as if frozen in place."
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I attack the shadowy figure"},
    ))

    assert _missed_change_warnings(received, player_id), (
        "narration describing a condition change with no tool call should warn the player their sheet may be stale"
    )


async def test_missed_change_heuristic_silent_when_narration_has_no_trigger_language():
    # A plain narrated miss/no-op shouldn't trip the heuristic just because
    # no tool call happened - most turns correctly involve no mechanical change.
    dm = NarratesFixedTextDM("You glance around the room but find nothing of note.")
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert not _missed_change_warnings(received, player_id)


async def test_missed_change_self_correction_reached_without_trigger_language():
    # check_missed_change() is no longer gated behind
    # POSSIBLE_UNTRACKED_CHANGE_PATTERN (see server/engine.py) - it's a
    # second, independent detection channel meant to catch exactly the
    # phrasing the narrow regex misses, so it must still be asked even on a
    # turn the regex wouldn't have flagged.
    dm = NarratesThenSelfCorrectsDM(
        "The merchant sizes you up and quietly slips a dagger from his sleeve, driving it home.",
        correction={"target": "self", "hp_delta": -3},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I haggle with the merchant"},
    ))

    assert dm.check_missed_change_calls, "check_missed_change should run even without regex-trigger language"
    assert _missed_change_corrections(received, player_id)


async def test_missed_change_self_correction_silent_when_nothing_changed_and_no_trigger_language():
    # The other half of the same guarantee: asking on every quiet turn must
    # not turn into noise when the backend genuinely finds nothing to fix.
    dm = NarratesThenSelfCorrectsDM(
        "You glance around the room but find nothing of note.", correction=None,
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert dm.check_missed_change_calls
    assert not _missed_change_corrections(received, player_id)
    assert not _missed_change_warnings(received, player_id), (
        "the passive warning stays regex-gated - a quiet turn shouldn't warn just because "
        "check_missed_change was asked"
    )


async def test_missed_change_heuristic_does_not_fire_during_opening_scene():
    # An opening scene routinely sets flavor using this heuristic's own
    # trigger words (a village recently attacked, a wounded NPC met in
    # passing) with no mechanical change ever expected on turn zero - a
    # real false-positive class this deliberately guards against.
    #
    # A real gap found while touching this file for something unrelated
    # (the save-failure work below): since the pre-game lobby slice, join()
    # no longer triggers the opening scene at all - only an explicit
    # start_session does (see test_opening_scene_fires_on_explicit_start_
    # not_on_join, above). This test previously only called join(), so its
    # assertion was trivially true regardless of whether the false-positive
    # guard actually worked - it never exercised the opening scene path it
    # claims to test. Fixed by actually starting the session.
    dm = NarratesFixedTextDM(
        "The village still bears the scars of a recent raid - a wounded farmer nurses a bleeding arm nearby."
    )
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())

    await join(engine, player_id)
    await start_session(engine, player_id)

    assert not _missed_change_warnings(received, player_id)


async def test_missed_change_advisory_carries_proposed_change_when_backend_offers_one():
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding.",
        correction=None,
        proposal={"target": "bandit", "hp_delta": -4},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    warnings = _missed_change_warnings(received, player_id)
    assert warnings, "the heuristic should still warn when the DM declines to auto-correct"
    assert warnings[0][3].get("proposed_change") == {"target": "bandit", "hp_delta": -4}
    assert dm.propose_correction_calls, "propose_correction should have been asked"
    assert "bandit" not in session.npcs, "the proposal must not be applied until the player confirms"


async def test_apply_proposed_change_applies_confirmed_npc_proposal():
    dm = NarratesThenSelfCorrectsDM(
        "Your blade finds its mark - the bandit staggers, bleeding.",
        correction=None,
        proposal={"target": "bandit", "hp_delta": -4},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))

    await engine.handle(Envelope(
        type="apply_proposed_change", session_id="test-session", sender_id=player_id, payload={}
    ))

    assert session.npcs["bandit"].hp == 6, "DEFAULT_NPC_HP 10 - 4 once the player confirms"
    npc_updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert npc_updates, "a confirmed proposal should broadcast like a real tool call"
    confirms = [r for r in received if r[0] == "send_to" and r[1] == player_id
                and r[2] == "system_message" and "Correction applied" in r[3]["text"]]
    assert confirms


async def test_apply_proposed_change_applies_confirmed_self_proposal():
    dm = NarratesThenSelfCorrectsDM(
        "A dart sinks into your shoulder - you bleed freely.",
        correction=None,
        proposal={"hp_delta": -3},
    )
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I dodge the trap"},
    ))

    await engine.handle(Envelope(
        type="apply_proposed_change", session_id="test-session", sender_id=player_id, payload={}
    ))

    assert session.characters[player_id].hp == 7, "default 10 - 3 once confirmed"
    char_updates = [r for r in received if r[0] == "send_to" and r[1] == player_id
                    and r[2] == "character_update"]
    assert char_updates


async def test_apply_proposed_change_with_nothing_pending_informs_player():
    dm = NarratesFixedTextDM("You walk quietly through the empty hall.")
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    await engine.handle(Envelope(
        type="apply_proposed_change", session_id="test-session", sender_id=player_id, payload={}
    ))

    replies = [r for r in received if r[0] == "send_to" and r[1] == player_id
               and r[2] == "system_message" and "no longer pending" in r[3]["text"]]
    assert replies


async def test_proposed_change_expires_on_next_turn():
    class ProposesThenGoesQuietDM(NarratesThenSelfCorrectsDM):
        def __init__(self):
            super().__init__("", correction=None, proposal={"target": "bandit", "hp_delta": -4})
            self._turns = 0

        async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
            self._turns += 1
            if self._turns == 1:
                yield "Your blade finds its mark - the bandit staggers, bleeding."
            else:
                yield "You rest and catch your breath in the quiet clearing."

    dm = ProposesThenGoesQuietDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I strike the bandit"},
    ))
    assert _missed_change_warnings(received, player_id), "first turn should propose a correction"

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I rest"},
    ))

    await engine.handle(Envelope(
        type="apply_proposed_change", session_id="test-session", sender_id=player_id, payload={}
    ))

    assert "bandit" not in session.npcs, "the stale proposal must not be applied after a new turn"
    replies = [r for r in received if r[0] == "send_to" and r[1] == player_id
               and r[2] == "system_message" and "no longer pending" in r[3]["text"]]
    assert replies


class FailingStore:
    """A real save-failure incident, reconstructed: ROADMAP.md documents a
    live process whose SESSION_STORE_DIR vanished mid-run (the repo it was
    launched from got moved), turning every _save() call into an unhandled
    FileNotFoundError that silently killed the connection with nothing
    shown to the player. This stands in for that same failure mode without
    needing a real vanishing directory."""

    def save(self, session):
        raise FileNotFoundError("sessions/test-session.json")

    def load(self, session_id):
        return None


def _save_failure_warnings(received: list[tuple], player_id: str) -> list[tuple]:
    return [
        r for r in received
        if r[0] == "send_to" and r[1] == player_id and r[3].get("level") == "warning"
        and "saving" in r[3].get("text", "")
    ]


async def test_join_warns_but_does_not_crash_when_save_fails():
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, StubDM(), broadcast, send_to, store=FailingStore())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain"},
    ))

    assert player_id in session.characters, "the character should still be created despite the save failure"
    assert _save_failure_warnings(received, player_id)
    # A real state_sync should still have gone out - a save failure doesn't
    # block the rest of the join.
    assert any(r[0] == "send_to" and r[2] == "state_sync" for r in received)


async def test_start_session_warns_but_still_starts_when_save_fails():
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, StubDM(), broadcast, send_to, store=FailingStore(), enable_opening_scene=False)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain"},
    ))
    received.clear()

    await engine.handle(Envelope(
        type="start_session", session_id="test-session", sender_id=player_id, payload={},
    ))

    assert session.started is True, "the session should still start despite the save failure"
    assert _save_failure_warnings(received, player_id)
    assert any(r[0] == "broadcast" and r[1] == "session_started" for r in received)


async def test_player_action_warns_but_still_advances_turn_when_save_fails():
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, StubDM(), broadcast, send_to, store=FailingStore(), enable_opening_scene=False)
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain"},
    ))
    await engine.handle(Envelope(
        type="start_session", session_id="test-session", sender_id=player_id, payload={},
    ))
    received.clear()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I open the door"},
    ))

    assert _save_failure_warnings(received, player_id)
    # The turn itself should still have resolved normally - narration
    # broadcast and a fresh turn_prompt, not silently dropped.
    assert any(r[0] == "broadcast" and r[1] == "turn_prompt" for r in received)


async def test_save_is_a_silent_no_op_with_no_store_configured():
    # The overwhelmingly common real path (store=None in every other test
    # in this file) shouldn't warn about a "failure" that never happened.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    assert not _save_failure_warnings(received, player_id)


async def test_character_edit_notes_updates_own_sheet_privately():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "notes", "value": "the old man owes me a favor"},
    ))

    assert session.characters[player_id].notes == "the old man owes me a favor"
    updates = [r for r in received if r[0] == "send_to" and r[2] == "character_update"]
    assert len(updates) == 1
    assert updates[0][3]["sheet_delta"]["notes"] == "the old man owes me a favor"
    # notes is never in the public view - no player_update/player_joined
    # broadcast should fire for a purely private bookkeeping edit.
    assert not any(r[0] == "broadcast" for r in received)


async def test_character_edit_add_item_appends_to_inventory():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "add_item", "value": "a shiny rock"},
    ))

    assert session.characters[player_id].find_item("a shiny rock") is not None


async def test_character_edit_remove_item_removes_a_present_item():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].add_item("a torch")

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "remove_item", "value": "a torch"},
    ))

    assert session.characters[player_id].find_item("a torch") is None


async def test_character_edit_remove_item_not_in_inventory_warns_and_makes_no_change():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "remove_item", "value": "a torch"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert not any(r[0] == "send_to" and r[2] == "character_update" for r in received)


async def test_build_starting_character_auto_equips_starting_weapon_and_armor():
    # Real tabletop chargen starts you already wielding/wearing your
    # starting gear, not carrying it unequipped until a player remembers
    # to run /equip - ROADMAP.md's equip/carry item.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))

    character = session.characters[player_id]
    assert character.equipped_weapon == "Longsword"
    assert character.equipped_armor == "Leather Armor"
    assert character.ac == 11 + character.stat_modifiers["dex"]  # 11 + Dex modifier, leather armor's real SRD AC


async def test_character_edit_equip_switches_weapon_and_does_not_touch_ac():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))
    session.characters[player_id].add_item("Shortbow")
    original_ac = session.characters[player_id].ac
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Shortbow"},
    ))

    character = session.characters[player_id]
    assert character.equipped_weapon == "Shortbow"
    assert character.ac == original_ac  # a weapon swap never touches AC
    # A weapon swap doesn't change ac, so no public player_update should fire.
    assert not any(r[0] == "broadcast" and r[1] == "player_update" for r in received)


async def test_character_edit_equip_armor_recomputes_ac_and_broadcasts_player_update():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    # A blank/unrecognized class starts unarmored (ac=10, no starting gear)
    # so equipping real armor for the first time has a real before/after.
    await join(engine, player_id)
    session.characters[player_id].add_item("Leather Armor")
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Leather Armor"},
    ))

    character = session.characters[player_id]
    assert character.equipped_armor == "Leather Armor"
    assert character.ac == 11 + character.stat_modifiers.get("dex", 0)
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "player_update"]
    assert updates, "a real ac change should broadcast the public player_update"
    assert updates[-1][2]["ac"] == character.ac


async def test_character_edit_equip_shield_adds_its_bonus_to_ac():
    # A real second equipment slot (server/state.py's equipped_shield),
    # additive on top of armor rather than a replacement value - closes
    # the gap ATTRIBUTION.md's own equipment-coverage note flagged: a
    # shield couldn't be worn at all before this.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].add_item("Shield")
    ac_before = session.characters[player_id].ac
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Shield"},
    ))

    character = session.characters[player_id]
    assert character.equipped_shield == "Shield"
    assert character.ac == ac_before + 2
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "player_update"]
    assert updates and updates[-1][2]["ac"] == character.ac


async def test_character_edit_equip_carries_a_real_magic_armor_bonus_into_ac():
    # A magic armor's real InventoryItem.magic_bonus (granted via the DM's
    # own add_item + magic_bonus) applies the moment it's equipped, not
    # just at creation.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    character = session.characters[player_id]
    character.apply_update({"add_item": "Leather Armor", "magic_bonus": 1})
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Leather Armor"},
    ))

    assert character.equipped_armor == "Leather Armor"
    assert character.ac == 11 + character.stat_modifiers.get("dex", 0) + 1  # base + dex + the magic bonus


async def test_character_edit_add_item_never_grants_a_magic_bonus():
    # magic_bonus is the DM tool's own field (update_character), never
    # player-settable through character_edit - the same "engine/DM
    # decides mechanical state" boundary equip/unequip's own ac side
    # effect already respects. A player has no way to send magic_bonus
    # over character_edit's {field, value} shape at all, but this locks
    # in the actual resulting item regardless of what a malformed/
    # adversarial payload might try.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "add_item", "value": "Longsword", "magic_bonus": 5},
    ))

    item = session.characters[player_id].find_item("Longsword")
    assert item is not None
    assert item.magic_bonus == 0


async def test_character_edit_unequip_shield_removes_its_bonus_from_ac():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].add_item("Shield")
    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Shield"},
    ))
    ac_with_shield = session.characters[player_id].ac
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "unequip", "value": "Shield"},
    ))

    character = session.characters[player_id]
    assert character.equipped_shield is None
    assert character.ac == ac_with_shield - 2


async def test_character_edit_remove_item_that_is_the_equipped_shield_also_unequips_it():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].add_item("Shield")
    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Shield"},
    ))
    ac_with_shield = session.characters[player_id].ac
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "remove_item", "value": "Shield"},
    ))

    character = session.characters[player_id]
    assert character.equipped_shield is None
    assert character.find_item("Shield") is None
    assert character.ac == ac_with_shield - 2


async def test_character_edit_equip_item_not_owned_warns():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Longsword"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert session.characters[player_id].equipped_weapon is None


async def test_character_edit_equip_something_not_a_weapon_or_armor_warns():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].add_item("Potion of Healing")
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "equip", "value": "Potion of Healing"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert session.characters[player_id].equipped_weapon is None
    assert session.characters[player_id].equipped_armor is None


async def test_character_edit_unequip_clears_the_slot_and_recomputes_ac():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "unequip", "value": "Leather Armor"},
    ))

    character = session.characters[player_id]
    assert character.equipped_armor is None
    assert character.ac == 10 + character.stat_modifiers["dex"]  # back to unarmored
    assert any(r[0] == "broadcast" and r[1] == "player_update" for r in received)


async def test_character_edit_unequip_something_not_equipped_warns():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "unequip", "value": "Longsword"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings


async def test_character_edit_remove_item_that_is_equipped_also_unequips_it():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter"},
    ))
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "remove_item", "value": "Leather Armor"},
    ))

    character = session.characters[player_id]
    assert character.equipped_armor is None
    assert character.find_item("Leather Armor") is None
    assert character.ac == 10 + character.stat_modifiers["dex"]
    assert any(r[0] == "broadcast" and r[1] == "player_update" for r in received)


async def test_character_edit_rejects_a_mechanical_field_not_in_the_allowed_set():
    # hp/conditions/stats/xp/ac stay DM- or engine-only - character_edit
    # only ever touches notes/inventory bookkeeping.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "hp", "value": 999},
    ))

    assert session.characters[player_id].hp == 10
    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert not any(r[0] == "send_to" and r[2] == "character_update" for r in received)


async def test_character_edit_is_exempt_from_turn_order():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id)
    await join(engine, other_id)
    assert session.current_turn != other_id

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=other_id,
        payload={"field": "notes", "value": "not my turn but this should still work"},
    ))

    assert session.characters[other_id].notes == "not my turn but this should still work"


async def test_character_edit_before_joining_warns_and_does_not_crash():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "notes", "value": "too early"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings


async def _death_save(engine, player_id):
    await engine.handle(Envelope(type="death_save", session_id="test-session", sender_id=player_id, payload={}))


async def test_public_character_view_includes_dying_and_dead():
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].dying = True

    view = _public_character_view(session.characters[player_id])
    assert view["dying"] is True
    assert view["dead"] is False


async def test_public_character_view_includes_race():
    # Race is the same "visible fluff, not hidden bookkeeping" treatment
    # class already gets - another player at the table would see it.
    engine, session, _ = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Rook", "character_class": "fighter", "race": "dwarf"},
    ))

    view = _public_character_view(session.characters[player_id])
    assert view["race"] == "Dwarf"


async def test_player_action_is_rejected_while_dying():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True
    received.clear()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id, payload={"text": "I get up"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert "deathsave" in warnings[0][3]["text"]
    assert not any(r[0] == "broadcast" and r[1] == "log_entry" for r in received)  # no narration attempted


async def test_player_action_is_rejected_while_dead():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dead = True
    received.clear()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id, payload={"text": "I get up"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert "died" in warnings[0][3]["text"]


async def test_player_action_is_rejected_while_stable_but_unconscious():
    # hp == 0 with neither dying nor dead - reached via 3 death-save
    # successes (or a same-turn stabilize-then-drop edge case) - still
    # can't act, distinct message from the actively-dying case.
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    received.clear()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id, payload={"text": "I get up"},
    ))

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert "needs healing" in warnings[0][3]["text"]


async def test_narrate_and_apply_announces_entering_dying():
    dm = UpdateCharacterDM({"hp_delta": -10})  # from full HP straight to 0
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id, payload={"text": "I charge in"},
    ))

    assert session.characters[player_id].dying is True
    announcements = [
        r for r in received
        if r[0] == "broadcast" and r[1] == "system_message" and "dying" in r[2]["text"]
    ]
    assert announcements


async def test_death_save_before_dying_warns():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    received.clear()

    await _death_save(engine, player_id)

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert not any(r[0] == "broadcast" and r[1] == "dice_result" for r in received)


async def test_death_save_after_death_warns_and_does_not_reroll():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dead = True
    received.clear()

    await _death_save(engine, player_id)

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings
    assert "already died" in warnings[0][3]["text"]


async def test_death_save_natural_20_revives_with_one_hp():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True
    received.clear()

    with patch("server.dice.random.randint", return_value=20):
        await _death_save(engine, player_id)

    character = session.characters[player_id]
    assert character.hp == 1
    assert character.dying is False
    results = [r for r in received if r[0] == "broadcast" and r[1] == "dice_result"]
    assert results[0][2]["result"] == 20
    assert results[0][2]["success"] is True


async def test_death_save_success_below_three_does_not_stabilize():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True

    with patch("server.dice.random.randint", return_value=15):
        await _death_save(engine, player_id)

    character = session.characters[player_id]
    assert character.death_save_successes == 1
    assert character.dying is True


async def test_death_save_third_success_stabilizes():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True
    session.characters[player_id].death_save_successes = 2
    received.clear()

    with patch("server.dice.random.randint", return_value=15):
        await _death_save(engine, player_id)

    character = session.characters[player_id]
    assert character.dying is False
    assert character.dead is False
    infos = [r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "stabilizes" in r[2]["text"]]
    assert infos


async def test_death_save_third_failure_kills():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True
    session.characters[player_id].death_save_failures = 2
    received.clear()

    with patch("server.dice.random.randint", return_value=5):
        await _death_save(engine, player_id)

    character = session.characters[player_id]
    assert character.dead is True
    assert character.dying is False
    errors = [r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "has died" in r[2]["text"]]
    assert errors
    assert errors[0][2]["level"] == "warning"


async def test_death_save_natural_one_counts_as_two_failures():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].hp = 0
    session.characters[player_id].dying = True

    with patch("server.dice.random.randint", return_value=1):
        await _death_save(engine, player_id)

    assert session.characters[player_id].death_save_failures == 2


async def test_death_save_is_exempt_from_turn_order():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    await join(engine, player_id)
    await join(engine, other_id)
    assert session.current_turn != other_id
    session.characters[other_id].hp = 0
    session.characters[other_id].dying = True

    with patch("server.dice.random.randint", return_value=15):
        await _death_save(engine, other_id)

    assert session.characters[other_id].death_save_successes == 1


async def test_reconnect_into_a_started_session_queues_a_pending_dm_recap():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)

    await join(engine, player_id)  # reconnect, session already underway

    assert session.pending_dm_recap == [player_id]


async def test_a_brand_new_player_joining_an_in_progress_session_does_not_queue_a_dm_recap():
    # pending_dm_recap exists to re-ground a returning player's *own*
    # history that's since scrolled out of the rolling window - a
    # brand-new player has no prior turns of their own to have lost, so
    # nothing needs re-grounding for them (they still get their own
    # player-facing _resume_recap() message, just not this DM-facing one).
    engine, session, received = make_engine(StubDM())
    first_player = str(uuid.uuid4())
    await join(engine, first_player)
    await start_session(engine, first_player)

    new_player = str(uuid.uuid4())
    await join(engine, new_player)

    assert session.pending_dm_recap == []


async def test_reconnect_before_session_started_does_not_queue_a_dm_recap():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await join(engine, player_id)  # reconnect, still pre-game

    assert session.pending_dm_recap == []


async def test_reconnecting_players_next_action_gets_the_recap_prepended_for_the_dm():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    session.world.summary = "The party discovered a hidden shrine beneath the ruins."

    await join(engine, player_id)  # reconnect after a long gap

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I check my map"},
    ))

    assert len(dm.action_texts) == 1
    assert "hidden shrine beneath the ruins" in dm.action_texts[0]
    assert dm.action_texts[0].endswith("I check my map")
    # Invisible to the rest of the table - the visible action log line
    # comes from the envelope's own raw text, not the recap-prefixed text
    # the DM itself received (same discipline as the content-preference
    # hint's own test above).
    action_lines = [
        r[2]["text"] for r in received
        if r[0] == "broadcast" and r[2].get("kind") == "action"
    ]
    assert action_lines == ["Thrain: I check my map"]


async def test_reconnect_recap_is_only_prepended_once():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)
    await join(engine, player_id)  # reconnect

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I check my map"},
    ))
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I keep moving"},
    ))

    assert len(dm.action_texts) == 2
    assert "Context:" not in dm.action_texts[1]
    assert dm.action_texts[1] == "I keep moving"
    assert session.pending_dm_recap == []
