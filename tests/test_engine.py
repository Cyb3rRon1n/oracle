from __future__ import annotations

import uuid

import pytest

from server.engine import GameEngine
from server.state import Session
from shared.protocol import Envelope


class StubDM:
    async def narrate(self, world_summary, character_summary, action_text):
        for word in ["You ", "swing ", "your ", "sword."]:
            yield word


class FailingDM:
    async def narrate(self, world_summary, character_summary, action_text):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator


def make_engine(dm):
    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env: Envelope):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env: Envelope):
        received.append(("send_to", pid, env.type, env.payload))

    return GameEngine(session, dm, broadcast, send_to), session, received


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
