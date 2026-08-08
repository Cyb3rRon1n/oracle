from __future__ import annotations

import asyncio
import uuid

import pytest
from websockets.asyncio.client import connect

from client.app import DungeonMasterApp, LobbyScreen, SessionScreen, WelcomeScreen
from server.engine import GameEngine
from server.state import Session
from server.transport import Transport
from shared.protocol import Envelope


class StubDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "ok."


async def test_join_over_real_websocket():
    session = Session(session_id="e2e-session")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, StubDM(), broadcast, send_to)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8799))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        ws = await connect("ws://localhost:8799")
        try:
            join = Envelope(
                type="join_session", session_id="e2e-session", sender_id=player_id,
                payload={"player_name": "Rook"},
            )
            await ws.send(join.to_json())

            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            env = Envelope.from_json(raw)
            assert env.type == "state_sync"
            assert env.payload["characters"][player_id]["name"] == "Rook"
        finally:
            await ws.close()
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def _recv_until(ws, event_type: str, timeout: float = 5) -> Envelope:
    """Drains messages off a real connection until one of the given type
    shows up - join/presence broadcasts interleave with other traffic
    (system_message, turn_prompt, ...), so a plain single recv() isn't
    reliable here."""
    async with asyncio.timeout(timeout):
        while True:
            raw = await ws.recv()
            env = Envelope.from_json(raw)
            if env.type == event_type:
                return env


async def test_second_player_sees_first_redacted_then_first_players_disconnect_broadcasts_player_left():
    session = Session(session_id="e2e-session-2")

    def engine_factory(broadcast, send_to):
        # enable_opening_scene=False: keeps this test's message ordering
        # predictable - a real opening scene would interleave several extra
        # log_entry broadcasts between join and turn_prompt.
        return GameEngine(session, StubDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8800))
    await asyncio.sleep(0.3)  # let the server bind

    player1_id = str(uuid.uuid4())
    player2_id = str(uuid.uuid4())
    try:
        ws1 = await connect("ws://localhost:8800")
        ws2 = await connect("ws://localhost:8800")
        try:
            await ws1.send(Envelope(
                type="join_session", session_id="e2e-session-2", sender_id=player1_id,
                payload={"player_name": "Rook", "character_class": "fighter"},
            ).to_json())
            await _recv_until(ws1, "player_joined")  # Rook's own join broadcast, back to itself

            await ws2.send(Envelope(
                type="join_session", session_id="e2e-session-2", sender_id=player2_id,
                payload={"player_name": "Rowan"},
            ).to_json())

            sync = await _recv_until(ws2, "state_sync")
            rook_view = sync.payload["characters"][player1_id]
            assert rook_view["name"] == "Rook"
            assert rook_view["hp"] == rook_view["max_hp"] == 10
            assert "inventory" not in rook_view, "a real second connection must never receive another player's inventory"

            # A live player_joined broadcast for Rowan should also reach
            # Rook's still-open connection.
            joined = await _recv_until(ws1, "player_joined")
            assert joined.payload["player_id"] == player2_id
            assert joined.payload["name"] == "Rowan"

            await ws1.close()  # real socket close - the same event a crashed/quit client produces

            left = await _recv_until(ws2, "player_left")
            assert left.payload == {"player_id": player1_id, "name": "Rook"}
        finally:
            await ws2.close()
            if ws1.close_code is None:
                await ws1.close()
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class NarratesOpeningDM:
    """Stands in for a real LLM backend the same way StubDM does elsewhere
    in this file - this environment has no live Ollama/Anthropic access,
    so this is the strongest verification available: a real client
    (DungeonMasterApp, real ClientTransport, real websocket - no
    ClientTransport mocking, unlike tests/test_client_app.py) driven
    through a real server (real Transport/GameEngine) for the whole
    lobby -> start -> live-streamed-opening-scene flow, with only the
    narration content itself substituted."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "A cold wind sweeps through the village square."


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


async def _wait_until(predicate, timeout: float = 5, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


async def test_real_client_lobby_to_session_flow_over_real_websocket():
    """The one test in this project driving a real client through a real
    server end to end, not a mocked transport on one side or the other -
    catches exactly the class of bug unit-level tests on either side
    can't: a real wire-format mismatch, or the client/engine disagreeing
    about lobby-vs-session sequencing (see GameEngine._on_start_session's
    session_started-before-narration ordering, and the App.query_one()
    default-screen pitfall documented in client/app.py - both found while
    building this feature, neither would show up testing client or server
    in isolation)."""
    session = Session(session_id="e2e-session-3")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, NarratesOpeningDM(), broadcast, send_to, enable_opening_scene=True)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8801))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8801", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            assert isinstance(app.screen, WelcomeScreen)
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")

            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))
            assert isinstance(app.screen, LobbyScreen)

            await pilot.click("#start")

            await _wait_until(lambda: isinstance(app.screen, SessionScreen))
            await _wait_until(lambda: "cold wind" in _log_text(app.screen.query_one("#log")))
            assert "cold wind" in _log_text(app.screen.query_one("#log"))
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
