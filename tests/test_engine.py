from __future__ import annotations

import uuid

import pytest

from server.engine import GameEngine
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


async def test_opening_scene_fires_once_on_a_genuine_campaign_start():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    assert len(dm.action_texts) == 1
    assert "adventure begins" in dm.action_texts[0]
    narration_chunks = [r for r in received if r[0] == "broadcast" and r[2].get("kind") == "narration"]
    assert narration_chunks, "the opening scene should stream narration like a real turn"
    assert session.log[-1]["text"] == "Scene."
    # doesn't consume the player's real first turn
    assert session.current_turn == player_id


async def test_opening_scene_does_not_fire_for_a_second_player_joining_in_progress():
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm, enable_opening_scene=True)
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())

    await join(engine, first_id)  # campaign start: fires once
    assert len(dm.action_texts) == 1

    await join(engine, second_id)  # session already has log entries: should not fire again

    assert len(dm.action_texts) == 1


async def test_opening_scene_disabled_by_default_in_make_engine_helper():
    # Regression guard for the test helper itself: most existing tests in
    # this file rely on join() having no narration side effect.
    dm = OpeningSceneDM()
    engine, session, received = make_engine(dm)
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    assert dm.action_texts == []


async def test_opening_scene_failure_warns_but_does_not_block_join():
    engine, session, received = make_engine(FailingDM(), enable_opening_scene=True)
    player_id = str(uuid.uuid4())

    await join(engine, player_id)

    warnings = [r for r in received if r[0] == "send_to" and r[3].get("level") == "warning"]
    assert warnings, "a failed opening scene should warn, not crash the join"
    assert session.current_turn == player_id, "the player should still be seated normally"
    assert session.log == [], "a failed opening scene shouldn't leave a partial log entry"
