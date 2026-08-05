from __future__ import annotations

from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect

from shared.protocol import Envelope


class ClientTransport:
    def __init__(self, uri: str, session_id: str, player_id: str):
        self._uri = uri
        self._session_id = session_id
        self._player_id = player_id
        self._ws: ClientConnection | None = None

    async def connect(self) -> None:
        self._ws = await connect(self._uri)

    async def send(self, type_: str, payload: dict) -> None:
        assert self._ws is not None
        envelope = Envelope(type=type_, session_id=self._session_id, sender_id=self._player_id, payload=payload)
        await self._ws.send(envelope.to_json())

    async def messages(self) -> AsyncIterator[Envelope]:
        assert self._ws is not None
        async for raw in self._ws:
            yield Envelope.from_json(raw)
