from __future__ import annotations

import asyncio
import uuid

import pytest
from websockets.asyncio.client import connect

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
