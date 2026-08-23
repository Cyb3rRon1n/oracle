"""End-to-end transport tests - a real WebSocket client against an
in-process Transport + GameEngine, replacing the TUI-era pilot harness
(test_transport_e2e.py) deleted in the v2 cutover. Same multiplayer
guarantees, no terminal dependency: join/state_sync, owner-view redaction,
chat broadcast, turn enforcement, and the protocol-v2 context round trip."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import os
import socket

import pytest
from websockets.asyncio.client import connect

from server.engine import GameEngine
from server.state import Session
from server.transport import Transport


class _ScriptedDM:
    supports_scene_facts = True

    async def narrate(self, history, character_summary, action_text, apply_update,
                      request_roll=None, update_world=None, world_summary=None, scene_sink=None):
        yield f"DM heard: {action_text}"

    async def summarize(self, prior_summary, turns):
        return "recap"


@contextlib.asynccontextmanager
async def running_server(port: int, world_context_dir: str | None = None):
    """One Transport on a real socket - the same stack server/main.py runs."""

    def factory(session_id: str, broadcast, send_to) -> GameEngine:
        return GameEngine(Session(session_id=session_id), _ScriptedDM(), broadcast, send_to)

    transport = Transport(factory)
    task = asyncio.create_task(transport.serve(host="127.0.0.1", port=port))
    await asyncio.sleep(0.2)
    try:
        yield port
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port: int, player_id: str):
        self.port = port
        self.player_id = player_id
        self.ws = None
        self.inbox: list[dict] = []

    async def __aenter__(self):
        self.ws = await connect(f"ws://127.0.0.1:{self.port}")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def send(self, type_: str, payload: dict, session_id: str = "t"):
        await self.ws.send(
            json.dumps(
                {
                    "type": type_,
                    "session_id": session_id,
                    "sender_id": self.player_id,
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "payload": payload,
                }
            )
        )

    async def recv_until(self, predicate, timeout: float = 3.0) -> dict | None:
        """Drain messages until one matches; already-matching inbox entries win."""
        for env in self.inbox:
            if predicate(env):
                return env
        async with asyncio.timeout(timeout):
            while True:
                env = json.loads(await self.ws.recv())
                self.inbox.append(env)
                if predicate(env):
                    return env

    async def drain_briefly(self, seconds: float = 0.4) -> list[dict]:
        """Collect whatever arrives in the window - for negative assertions."""
        got: list[dict] = []
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                while True:
                    env = json.loads(await self.ws.recv())
                    self.inbox.append(env)
                    got.append(env)
        return got


async def join(client: Client, name: str) -> dict:
    await client.send("join_session", {"player_name": name})
    return await client.recv_until(lambda e: e["type"] == "state_sync")


async def test_two_players_share_one_session_with_redaction():
    port = free_port()
    async with running_server(port):
        async with Client(port, "p-a") as a, Client(port, "p-b") as b:
            sync_a = await join(a, "Aria")
            assert sync_a["payload"]["characters"]["p-a"]["name"] == "Aria"
            # presence flows both ways once B joins
            await asyncio.sleep(0.1)
            sync_b = await join(b, "Bod")
            view_of_a = sync_b["payload"]["characters"]["p-a"]
            assert "inventory" not in view_of_a and "stats" not in view_of_a and "notes" not in view_of_a
            assert "inventory" in sync_a["payload"]["characters"]["p-a"]  # owner view stays full


async def test_chat_broadcasts_and_out_of_turn_actions_reject():
    port = free_port()
    async with running_server(port):
        async with Client(port, "p-a") as a, Client(port, "p-b") as b:
            await join(a, "Aria")
            await join(b, "Bod")

            await b.send("chat_message", {"text": "Sulfur?"})
            got = await a.recv_until(lambda e: e["type"] == "log_entry" and e["payload"].get("kind") == "chat")
            assert "Sulfur?" in got["payload"]["text"]

            await a.send("start_session", {})
            started = await a.recv_until(lambda e: e["type"] == "session_started")
            assert started is not None
            turn_env = await a.recv_until(lambda e: e["type"] == "turn_prompt")
            current = turn_env["payload"]["player_id"]

            actor = b if current == "p-a" else a
            await actor.send("player_action", {"text": "I swing wildly!"})
            warning = await actor.recv_until(
                lambda e: e["type"] == "system_message" and "turn" in str(e["payload"].get("text", "")).lower()
            )
            assert warning is not None


async def test_context_manifest_and_select_round_trip(tmp_path, monkeypatch):
    lore_dir = tmp_path / "world_context"
    lore_dir.mkdir()
    (lore_dir / "gates.md").write_text("# Gate\nThe warden watches.", encoding="utf-8")
    # The engine resolves WORLD_CONTEXT_DIR when its Session's first GameEngine
    # is lazily constructed - patch before any join touches this port.
    monkeypatch.setenv("WORLD_CONTEXT_DIR", str(lore_dir))

    port = free_port()
    async with running_server(port):
        async with Client(port, "p-a") as a:
            await join(a, "Aria")
            await a.send("context_manifest_request", {})
            manifest = await a.recv_until(lambda e: e["type"] == "context_manifest")
            names = [f["name"] for f in manifest["payload"]["files"]]
            assert names == ["gates.md"]
            assert all("content" not in f for f in manifest["payload"]["files"])  # listing only

            await a.send("context_select", {"files": ["gates.md"]})
            messages = await a.drain_briefly()
            warnings = [m for m in messages if m["type"] == "system_message" and "Ignored unknown" in str(m["payload"].get("text", ""))]
            assert warnings == []  # valid selection produces no warning
