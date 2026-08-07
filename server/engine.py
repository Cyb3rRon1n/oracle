from __future__ import annotations

from collections.abc import Awaitable, Callable

from shared.protocol import Envelope

from . import dice
from .narrator import NarratorBackend
from .persistence import SessionStore
from .state import CharacterSheet, Session

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]

# Mirrors _on_join_session's own hp=10, max_hp=10 fallback for a fresh
# player character - the safety net for an NPC introduced without a
# real max_hp from lookup_rule, not the intended path.
DEFAULT_NPC_HP = 10


class GameEngine:
    """Owns session state and enforces the strict turn queue (docs/protocol.md)."""

    def __init__(
        self,
        session: Session,
        dm: NarratorBackend,
        broadcast: Broadcast,
        send_to: SendTo,
        store: SessionStore | None = None,
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store

    def _save(self) -> None:
        if self._store is not None:
            self._store.save(self._session)

    async def handle(self, envelope: Envelope) -> None:
        handler = getattr(self, f"_on_{envelope.type}", None)
        if handler is not None:
            await handler(envelope)

    async def _on_join_session(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        if player_id not in self._session.characters:
            name = envelope.payload.get("player_name", player_id)
            self._session.characters[player_id] = CharacterSheet(
                player_id=player_id, name=name, hp=10, max_hp=10
            )
            self._session.turn_order.append(player_id)
            self._save()

        character_name = self._session.characters[player_id].name
        await self._send_to(player_id, self._state_sync_envelope())
        await self._broadcast(self._system_envelope(f"{character_name} joined the session.", level="info"))
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

        sheet_changed = False
        npcs_touched: set[str] = set()

        def apply_update(update: dict) -> str:
            nonlocal sheet_changed

            target = update.get("target") or "self"

            # A model given the character sheet as JSON (which includes its own
            # player_id) sometimes echoes that literal id back as target instead
            # of "self" - without this, that misroutes into the NPC branch below
            # and silently creates a phantom NPC sheet named after the player_id.
            if target == "self" or target == player_id:
                result = character.apply_update(update)
                if not result.startswith("No changes applied"):
                    sheet_changed = True
                return result

            npc = self._session.npcs.get(target)
            introduced = npc is None

            if introduced:
                # Same default-HP fallback join_session already uses for a
                # fresh player character - a safety net for when the DM
                # forgets to pass a real max_hp from lookup_rule, not the
                # intended path.
                max_hp = update.get("max_hp") or DEFAULT_NPC_HP
                npc = CharacterSheet(player_id=target, name=target, hp=max_hp, max_hp=max_hp)
                self._session.npcs[target] = npc

            delta_result = npc.apply_update(update)
            changed = not delta_result.startswith("No changes applied")

            # Introducing a new NPC is itself a real change worth
            # broadcasting even if this same call's deltas were a no-op
            # (e.g. just naming it with no damage yet) - matches the
            # player-character path's own "only broadcast on a real
            # change" rule otherwise.
            if introduced or changed:
                npcs_touched.add(target)

            if introduced:
                intro = f"Introduced {target} (HP {npc.hp}/{npc.max_hp})."
                return f"{intro} {delta_result}" if changed else intro

            return delta_result

        buffer = ""
        try:
            async for chunk in self._dm.narrate(
                history=self._session.history,
                character_summary=character.model_dump_json(),
                action_text=text,
                apply_update=apply_update,
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
        self._session.append_turn(text, buffer)

        if sheet_changed:
            await self._send_to(player_id, self._character_update_envelope(player_id, character))

        for name in npcs_touched:
            await self._broadcast(self._npc_update_envelope(name, self._session.npcs[name]))

        self._session.advance_turn()
        self._save()
        await self._broadcast(self._turn_prompt_envelope())

    async def _on_chat_message(self, envelope: Envelope) -> None:
        await self._broadcast(self._log_envelope("chat", envelope.payload.get("text", "")))

    async def _on_dice_roll(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        name = character.name if character else player_id
        notation = envelope.payload.get("dice", "")
        reason = envelope.payload.get("reason", "")

        try:
            total, rolls = dice.roll(notation)
        except dice.InvalidDiceNotation as exc:
            await self._send_to(player_id, self._system_envelope(str(exc), level="warning"))
            return

        label = f" ({reason})" if reason else ""
        await self._broadcast(
            self._log_envelope("dice", f"{name} rolls {notation}{label}: {total} {rolls}")
        )
        await self._broadcast(
            Envelope(
                type="dice_result",
                session_id=self._session.session_id,
                sender_id="server",
                payload={
                    "roller_id": player_id,
                    "dice": notation,
                    "result": total,
                    "rolls": rolls,
                    "purpose": reason,
                },
            )
        )

    def _state_sync_envelope(self) -> Envelope:
        return Envelope(
            type="state_sync",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                "characters": {pid: c.model_dump() for pid, c in self._session.characters.items()},
                "npcs": {name: npc.model_dump() for name, npc in self._session.npcs.items()},
                "world_state": self._session.world.model_dump(),
                "turn_order": self._session.turn_order,
                "current_turn": self._session.current_turn,
                "log_tail": self._session.log[-20:],
            },
        )

    def _character_update_envelope(self, player_id: str, character: CharacterSheet) -> Envelope:
        return Envelope(
            type="character_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "sheet_delta": character.model_dump()},
        )

    def _npc_update_envelope(self, name: str, npc: CharacterSheet) -> Envelope:
        # Broadcast, not routed privately like _character_update_envelope -
        # an NPC's wounds/conditions are shared observable fiction, not a
        # single player's own private sheet.
        return Envelope(
            type="npc_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"name": name, "sheet_delta": npc.model_dump()},
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
