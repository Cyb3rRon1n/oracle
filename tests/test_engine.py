from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from server.engine import GameEngine, build_starting_character, _compute_ac
from server.rules import RulesIndex
from server.state import Session
from shared.protocol import Envelope


class StubDM:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        self.calls.append(list(history))  # snapshot — engine mutates session.history in place after this call
        for word in ["You ", "swing ", "your ", "sword."]:
            yield word


class FailingDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator


class UpdateCharacterDM:
    """Narrates a fixed line and calls apply_update with the given tool input,
    simulating the DM invoking the update_character tool mid-turn."""

    def __init__(self, update: dict):
        self._update = update
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        self.tool_result = apply_update(self._update)
        yield "You feel the effects immediately."


class UpdateSequenceDM:
    """Calls apply_update once per dict in updates, in order, simulating
    several update_character tool calls within a single DM turn (or, across
    separate .handle() calls, across separate turns)."""

    def __init__(self, updates: list[dict]):
        self._updates = updates
        self.tool_results: list[str] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        for update in self._updates:
            self.tool_results.append(apply_update(update))
        yield "Something happens."


class RequestRollDM:
    """Calls request_roll with the given tool input, simulating the DM
    invoking request_roll mid-turn, then narrates."""

    def __init__(self, roll_input: dict):
        self._roll_input = roll_input
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        self.tool_result = request_roll(self._roll_input)
        yield "You attempt it."


class UpdateWorldDM:
    """Calls update_world with the given tool input, simulating the DM
    invoking update_world mid-turn, then narrates."""

    def __init__(self, world_update: dict):
        self._world_update = world_update
        self.tool_result: str | None = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        self.tool_result = update_world(self._world_update)
        yield "The story moves on."


class OpeningSceneDM:
    """Records the action_text of every narrate() call it receives, and
    always narrates the same fixed text - used to test the opening-scene
    hook without depending on real content."""

    def __init__(self):
        self.action_texts: list[str] = []

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        self.action_texts.append(action_text)
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
    assert sheet.inventory == expected_inventory
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
    assert character.inventory == ["Longsword", "Leather Armor"]


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
        "inventory": ["Mace", "Holy Symbol"],
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
    assert character.inventory == ["Mace", "Holy Symbol"]
    assert character.notes == "Sworn to protect the village of Rivenwood."
    assert character.xp == 450
    assert character.level == 3


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
    assert "torch" in character.inventory
    assert "HP -4" in dm.tool_result

    updates = [
        r for r in received
        if r[0] == "send_to" and r[2] == "character_update" and r[1] == player_id
    ]
    assert updates, "a real sheet change should push a character_update to the player"
    assert updates[-1][3]["sheet_delta"]["hp"] == 6
    assert updates[-1][3]["sheet_delta"]["inventory"] == ["torch"]


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
    assert private_updates[-1][3]["sheet_delta"]["inventory"] == ["torch"]

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
    assert _compute_ac([], dex_modifier=2, rules=rules) == 12
    assert _compute_ac(["Potion of Healing"], dex_modifier=1, rules=rules) == 11  # no armor in inventory


def test_compute_ac_uses_the_best_matched_armors_base_value():
    rules = RulesIndex.load_default()
    assert _compute_ac(["Leather Armor"], dex_modifier=1, rules=rules) == 12  # 11 + 1
    # Case/whitespace shouldn't matter - the same _slug()-based lookup
    # every other equipment/monster name match in this project already uses.
    assert _compute_ac(["leather armor"], dex_modifier=0, rules=rules) == 11


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


async def start_session(engine, player_id):
    await engine.handle(Envelope(
        type="start_session", session_id="test-session", sender_id=player_id, payload={},
    ))


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


async def test_rejoin_after_session_started_shows_turn_prompt():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await start_session(engine, player_id)

    received.clear()
    await join(engine, player_id)  # e.g. the client restarted mid-game

    assert any(r for r in received if r[0] == "broadcast" and r[1] == "turn_prompt"), \
        "reconnecting into an already-started game should still show whose turn it is"


class NarratesFixedTextDM:
    """Narrates the given fixed text and, if update: a dict is provided,
    calls apply_update with it first - simulating a DM turn that may or may
    not actually invoke the tool, independent of what the narration says."""

    def __init__(self, text: str, update: dict | None = None):
        self._text = text
        self._update = update

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        if self._update is not None:
            apply_update(self._update)
        yield self._text


def _missed_change_warnings(received: list[tuple], player_id: str) -> list[tuple]:
    return [
        r for r in received
        if r[0] == "send_to" and r[1] == player_id and r[2] == "system_message"
        and "out of sync" in r[3]["text"]
    ]


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

    assert "a shiny rock" in session.characters[player_id].inventory


async def test_character_edit_remove_item_removes_a_present_item():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.characters[player_id].inventory.append("a torch")

    await engine.handle(Envelope(
        type="character_edit", session_id="test-session", sender_id=player_id,
        payload={"field": "remove_item", "value": "a torch"},
    ))

    assert "a torch" not in session.characters[player_id].inventory


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
