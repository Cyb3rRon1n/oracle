from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from websockets.asyncio.server import ServerConnection, serve

from shared.protocol import Envelope

from .engine import Broadcast, GameEngine, SendTo

logger = logging.getLogger(__name__)

EngineFactory = Callable[[str, Broadcast, SendTo], GameEngine]


def _listen_targets(host: str, port: int) -> list[tuple[str, int]]:
    if host in ("", "localhost"):
        return [("127.0.0.1", port), ("::1", port)]
    if host == "0.0.0.0":
        return [("0.0.0.0", port), ("::", port)]
    return [(host, port)]


class Transport:
    """One process, many concurrent games: each session_id gets its own
    GameEngine, created lazily on that session's first join_session and
    reused afterward (session_id -> GameEngine). Connections are tracked
    per player_id but each carries its own session_id alongside, so a
    broadcast for one session's engine only reaches that session's own
    connections - two unrelated games sharing a server never see each
    other's traffic. Previously there was exactly one GameEngine for the
    whole process (loaded from a single startup SESSION_ID env var), so
    every client that connected joined the same game regardless of what
    session_id it actually sent - a client-specified "join a different
    game" was silently ignored."""

    def __init__(self, engine_factory: EngineFactory):
        self._engine_factory = engine_factory
        self._engines: dict[str, GameEngine] = {}
        # player_id -> (session_id, connection)
        self._connections: dict[str, tuple[str, ServerConnection]] = {}

    def _get_or_create_engine(self, session_id: str) -> GameEngine:
        engine = self._engines.get(session_id)
        if engine is None:

            async def broadcast(envelope: Envelope) -> None:
                await self._broadcast(session_id, envelope)

            engine = self._engine_factory(session_id, broadcast, self._send_to)
            self._engines[session_id] = engine
        return engine

    async def _broadcast(self, session_id: str, envelope: Envelope) -> None:
        message = envelope.to_json()
        targets = [conn for sid, conn in self._connections.values() if sid == session_id]
        await asyncio.gather(*(conn.send(message) for conn in targets), return_exceptions=True)

    async def _send_to(self, player_id: str, envelope: Envelope) -> None:
        entry = self._connections.get(player_id)
        if entry is not None:
            _, conn = entry
            await conn.send(envelope.to_json())

    async def _handler(self, connection: ServerConnection) -> None:
        player_id: str | None = None
        session_id: str | None = None
        try:
            async for raw in connection:
                envelope = Envelope.from_json(raw)
                if envelope.type == "join_session":
                    player_id = envelope.sender_id
                    session_id = envelope.session_id
                    self._connections[player_id] = (session_id, connection)
                engine = self._get_or_create_engine(envelope.session_id)
                await engine.handle(envelope)
        finally:
            if player_id is not None:
                self._connections.pop(player_id, None)
                # The counterpart to _on_join_session's player_joined
                # broadcast - a disconnect isn't a client-sent event, so the
                # engine can't learn about it through the normal handle()/
                # envelope dispatch path; the transport is the only thing
                # that actually observes the socket closing.
                engine = self._engines.get(session_id) if session_id is not None else None
                if engine is not None:
                    await engine.handle_disconnect(player_id)

    async def _serve_one(self, host: str, port: int) -> None:
        async with serve(self._handler, host, port):
            logger.info("Server listening on ws://%s:%s", host, port)
            await asyncio.Future()

    async def serve(self, host: str = "localhost", port: int = 8765) -> None:
        # A browser that resolves "localhost" to ::1 can't reach a server bound
        # to 0.0.0.0 (IPv4-only): the page loads, the websocket dial is refused,
        # and the client spins on "reconnecting". Bind both address families for
        # ambiguous hosts; one failing bind (e.g. no IPv6 on the host) is logged
        # and never takes the other family down.
        results = await asyncio.gather(
            *(self._serve_one(h, p) for h, p in _listen_targets(host, port)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Listener failed to start: %s", result)
