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
