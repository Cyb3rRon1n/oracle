from __future__ import annotations

from collections.abc import Awaitable, Callable

from shared.protocol import Envelope

from .narrator import NarratorBackend
from .state import CharacterSheet, Session

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]


class GameEngine:
    """Owns session state and enforces the strict turn queue (docs/protocol.md)."""

    def __init__(self, session: Session, dm: NarratorBackend, broadcast: Broadcast, send_to: SendTo):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to

    async def handle(self, envelope: Envelope) -> None:
        handler = getattr(self, f"_on_{envelope.type}", None)
        if handler is not None:
            await handler(envelope)

    async def _on_join_session(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        name = envelope.payload.get("player_name", player_id)
        if player_id not in self._session.characters:
            self._session.characters[player_id] = CharacterSheet(
                player_id=player_id, name=name, hp=10, max_hp=10
            )
            self._session.turn_order.append(player_id)

        await self._send_to(player_id, self._state_sync_envelope())
        await self._broadcast(self._system_envelope(f"{name} joined the session.", level="info"))
        if self._session.current_turn == player_id:
            await self._broadcast(self._turn_prompt_envelope())

    async def _on_player_action(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        if player_id != self._session.current_turn:
            await self._send_to(player_id, self._system_envelope("It's not your turn.", level="warning"))
            return

        character = self._session.characters[player_id]
        text = envelope.payload.get("text", "")
        await self._broadcast(self._log_envelope("action", f"{character.name}: {text}"))

        buffer = ""
        try:
            async for chunk in self._dm.narrate(
                world_summary=self._session.world.summary,
                character_summary=character.model_dump_json(),
                action_text=text,
            ):
                buffer += chunk
                await self._broadcast(self._log_envelope("narration", chunk, done=False))
            await self._broadcast(self._log_envelope("narration", "", done=True))
        except Exception as exc:
            await self._send_to(
                player_id, self._system_envelope(f"The DM couldn't respond: {exc}", level="error")
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.world.summary = buffer

        self._session.advance_turn()
        await self._broadcast(self._turn_prompt_envelope())

    async def _on_chat_message(self, envelope: Envelope) -> None:
        await self._broadcast(self._log_envelope("chat", envelope.payload.get("text", "")))

    def _state_sync_envelope(self) -> Envelope:
        return Envelope(
            type="state_sync",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                "characters": {pid: c.model_dump() for pid, c in self._session.characters.items()},
                "world_state": self._session.world.model_dump(),
                "turn_order": self._session.turn_order,
                "current_turn": self._session.current_turn,
                "log_tail": self._session.log[-20:],
            },
        )

    def _turn_prompt_envelope(self) -> Envelope:
        return Envelope(
            type="turn_prompt",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": self._session.current_turn, "prompt_text": "What do you do?"},
        )

    def _log_envelope(self, kind: str, text: str, done: bool | None = None) -> Envelope:
        payload: dict = {"kind": kind, "text": text}
        if done is not None:
            payload["done"] = done
        return Envelope(type="log_entry", session_id=self._session.session_id, sender_id="server", payload=payload)

    def _system_envelope(self, text: str, level: str = "info") -> Envelope:
        return Envelope(
            type="system_message",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"level": level, "text": text},
        )
