from __future__ import annotations

import uuid

from server.engine import GameEngine
from server.persistence import JSONFileSessionStore
from server.state import CharacterSheet, Session
from shared.protocol import Envelope


class StubDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "You see nothing of note."


def test_round_trip_preserves_state(tmp_path):
    store = JSONFileSessionStore(tmp_path)
    session = Session(session_id="rt-session")
    session.characters["p1"] = CharacterSheet(player_id="p1", name="Rook", hp=7, max_hp=10)
    session.turn_order = ["p1"]
    session.log.append({"kind": "narration", "text": "The door creaks open."})

    store.save(session)
    loaded = store.load("rt-session")

    assert loaded is not None
    assert loaded.characters["p1"].name == "Rook"
    assert loaded.characters["p1"].hp == 7
    assert loaded.current_turn == "p1"
    assert loaded.log[-1]["text"] == "The door creaks open."


def test_load_missing_session_returns_none(tmp_path):
    store = JSONFileSessionStore(tmp_path)
    assert store.load("nonexistent") is None


async def test_engine_saves_after_join_and_after_action(tmp_path):
    store = JSONFileSessionStore(tmp_path)
    session = Session(session_id="engine-session")

    async def broadcast(env: Envelope):
        pass

    async def send_to(pid, env: Envelope):
        pass

    engine = GameEngine(session, StubDM(), broadcast, send_to, store=store)
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="engine-session", sender_id=player_id,
        payload={"player_name": "Rook"},
    ))
    assert store.load("engine-session") is not None, "join should persist a new character"

    await engine.handle(Envelope(
        type="player_action", session_id="engine-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))
    saved = store.load("engine-session")
    assert saved.log[-1]["text"] == "You see nothing of note."


async def test_server_resumes_existing_character_on_reconnect(tmp_path):
    """A player rejoining with the same player_id (e.g. after a client restart,
    since the client persists its own id) should get their existing character
    back, not a fresh one."""
    store = JSONFileSessionStore(tmp_path)
    session = Session(session_id="resume-session")

    async def broadcast(env: Envelope):
        pass

    async def send_to(pid, env: Envelope):
        pass

    engine = GameEngine(session, StubDM(), broadcast, send_to, store=store)
    player_id = str(uuid.uuid4())

    await engine.handle(Envelope(
        type="join_session", session_id="resume-session", sender_id=player_id,
        payload={"player_name": "Rook"},
    ))
    session.characters[player_id].hp = 3  # simulate damage taken during play
    store.save(session)

    # Simulate a fresh server process: load from disk, build a new engine on it.
    resumed_session = store.load("resume-session")
    resumed_engine = GameEngine(resumed_session, StubDM(), broadcast, send_to, store=store)

    await resumed_engine.handle(Envelope(
        type="join_session", session_id="resume-session", sender_id=player_id,
        payload={"player_name": "Rook"},
    ))

    assert resumed_session.characters[player_id].hp == 3
    assert len(resumed_session.turn_order) == 1, "rejoin must not create a duplicate character"
