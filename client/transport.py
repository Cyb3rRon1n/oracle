from __future__ import annotations

from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect

from shared.protocol import Envelope


class ClientTransport:
    """session_id/player_id are plain, mutable public attributes rather
    than constructor-fixed private ones - real server-owned identity
    (ROADMAP.md, 2026-08-13) means neither is known at connect() time
    anymore: the connection has to open and send a real login before the
    server ever hands back a player_id, and session_id isn't chosen
    until WelcomeScreen's own join flow runs afterward, over this same
    connection. DungeonMasterApp sets both directly once each becomes
    known (the same "app writes what it knows onto its own collaborator
    objects" pattern this client already uses for its own self.* state)."""

    def __init__(self, uri: str):
        self._uri = uri
        self.session_id: str = ""
        self.player_id: str = ""
        self._ws: ClientConnection | None = None

    async def connect(self) -> None:
        self._ws = await connect(self._uri)

    async def send(self, type_: str, payload: dict) -> None:
        assert self._ws is not None
        envelope = Envelope(type=type_, session_id=self.session_id, sender_id=self.player_id, payload=payload)
        await self._ws.send(envelope.to_json())

    async def messages(self) -> AsyncIterator[Envelope]:
        assert self._ws is not None
        async for raw in self._ws:
            yield Envelope.from_json(raw)
