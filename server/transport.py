from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from websockets.asyncio.server import ServerConnection, serve

from shared.protocol import Envelope

from .engine import Broadcast, GameEngine, SendTo

logger = logging.getLogger(__name__)

EngineFactory = Callable[[Broadcast, SendTo], GameEngine]


class Transport:
    def __init__(self, engine_factory: EngineFactory):
        self._engine_factory = engine_factory
        self._connections: dict[str, ServerConnection] = {}
        self._engine: GameEngine | None = None

    async def _broadcast(self, envelope: Envelope) -> None:
        message = envelope.to_json()
        await asyncio.gather(
            *(conn.send(message) for conn in self._connections.values()),
            return_exceptions=True,
        )

    async def _send_to(self, player_id: str, envelope: Envelope) -> None:
        conn = self._connections.get(player_id)
        if conn is not None:
            await conn.send(envelope.to_json())

    async def _handler(self, connection: ServerConnection) -> None:
        if self._engine is None:
            self._engine = self._engine_factory(self._broadcast, self._send_to)

        player_id: str | None = None
        try:
            async for raw in connection:
                envelope = Envelope.from_json(raw)
                if envelope.type == "join_session":
                    player_id = envelope.sender_id
                    self._connections[player_id] = connection
                await self._engine.handle(envelope)
        finally:
            if player_id is not None:
                self._connections.pop(player_id, None)
                # The counterpart to _on_join_session's player_joined
                # broadcast - a disconnect isn't a client-sent event, so the
                # engine can't learn about it through the normal handle()/
                # envelope dispatch path; the transport is the only thing
                # that actually observes the socket closing.
                await self._engine.handle_disconnect(player_id)

    async def serve(self, host: str = "localhost", port: int = 8765) -> None:
        async with serve(self._handler, host, port):
            logger.info("Server listening on ws://%s:%s", host, port)
            await asyncio.Future()
