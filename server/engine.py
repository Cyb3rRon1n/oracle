from __future__ import annotations

import re
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

# A visible mitigation, not a fix (ROADMAP.md's tool-call reliability
# investigation, item 6's remaining-candidates list) - the live qwen2.5:7b/
# llama3.1:8b runs documented there repeatedly narrated unambiguous lethal
# damage to an NPC with zero update_character call all turn. This doesn't
# make the model call the tool; it only tells the player their sheet may be
# out of sync with the fiction, so a silently-stale sheet isn't mistaken for
# a trustworthy one. Deliberately narrow and outcome-focused (confirmed
# damage/death/condition language) rather than any attack verb, to keep
# false positives down - a narrated *miss* shouldn't trip this. Still
# expect both false positives (a near-miss description using "wound" in
# passing) and false negatives (phrasing this doesn't catch) - it's a
# signal for the player to weigh, not a verdict.
POSSIBLE_UNTRACKED_CHANGE_PATTERN = re.compile(
    r"\b(damage|wound(?:s|ed|ing)?|bleed(?:s|ing)?|dies?|dead|death|slain|"
    r"kills?|killed|unconscious|collapses?|hp|health)\b",
    re.IGNORECASE,
)


class GameEngine:
    """Owns session state and enforces the strict turn queue (docs/protocol.md)."""

    def __init__(
        self,
        session: Session,
        dm: NarratorBackend,
        broadcast: Broadcast,
        send_to: SendTo,
        store: SessionStore | None = None,
        enable_opening_scene: bool = True,
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store
        self._enable_opening_scene = enable_opening_scene

    def _save(self) -> None:
        if self._store is not None:
            self._store.save(self._session)

    async def handle(self, envelope: Envelope) -> None:
        handler = getattr(self, f"_on_{envelope.type}", None)
        if handler is not None:
            await handler(envelope)

    async def _on_join_session(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        is_new_character = player_id not in self._session.characters
        # A genuine campaign start - not just this player joining an
        # already-started session - is the only time an opening scene makes
        # sense. Log still empty is the signal: nothing has happened yet.
        is_campaign_start = is_new_character and not self._session.log and self._enable_opening_scene

        if is_new_character:
            name = envelope.payload.get("player_name", player_id)
            self._session.characters[player_id] = CharacterSheet(
                player_id=player_id, name=name, hp=10, max_hp=10
            )
            self._session.turn_order.append(player_id)
            self._save()

        character = self._session.characters[player_id]
        await self._send_to(player_id, self._state_sync_envelope())
        await self._broadcast(self._system_envelope(f"{character.name} joined the session.", level="info"))

        if is_campaign_start:
            await self._narrate_opening_scene(character)

        if self._session.current_turn == player_id:
            await self._broadcast(self._turn_prompt_envelope())

    async def _narrate_opening_scene(self, character: CharacterSheet) -> None:
        """Best-effort: a missing opening scene shouldn't block joining, so
        failures here are reported but don't propagate like a real turn's
        would. Reuses the exact same narrate()/tool-wiring path a real turn
        uses, via a synthetic action_text, so the DM can set an initial
        location/objective with update_world exactly like any other turn.
        check_for_missed_changes=False: an opening scene routinely sets a
        scene using words this heuristic watches for (a village recently
        attacked, a wounded NPC met in passing) with no mechanical change
        ever expected on turn zero - a real false-positive class, not a
        hypothetical one."""
        try:
            buffer = await self._narrate_and_apply(
                character,
                "(The adventure begins - set an opening scene to draw the player in.)",
                check_for_missed_changes=False,
            )
        except Exception:
            await self._send_to(
                character.player_id,
                self._system_envelope("Couldn't generate an opening scene.", level="warning"),
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.append_turn("(The adventure begins.)", buffer)
        self._save()

    async def _on_player_action(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        if player_id != self._session.current_turn:
            await self._send_to(player_id, self._system_envelope("It's not your turn.", level="warning"))
            return

        character = self._session.characters[player_id]
        text = envelope.payload.get("text", "")
        await self._broadcast(self._log_envelope("action", f"{character.name}: {text}"))

        try:
            buffer = await self._narrate_and_apply(character, text)
        except Exception as exc:
            await self._send_to(
                player_id, self._system_envelope(f"The DM couldn't respond: {exc}", level="error")
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.append_turn(text, buffer)
        self._session.advance_turn()
        self._save()
        await self._broadcast(self._turn_prompt_envelope())

    async def _narrate_and_apply(
        self, character: CharacterSheet, action_text: str, check_for_missed_changes: bool = True
    ) -> str:
        """Runs one DM narrate() call for the given character/action, wiring
        up apply_update/request_roll/update_world, broadcasting narration and
        any resulting state changes exactly as a normal turn does. Returns
        the full narration text. Shared by _on_player_action (a real turn)
        and _narrate_opening_scene (a synthetic "turn" on campaign start that
        doesn't consume the turn queue)."""
        player_id = character.player_id
        sheet_changed = False
        npcs_touched: set[str] = set()
        rolls_made: list[dict] = []
        world_changed = False

        def request_roll(update: dict) -> str:
            notation = update.get("dice", "1d20")
            dc = update.get("dc")
            reason = update.get("reason", "")

            try:
                total, rolls = dice.roll(notation)
            except dice.InvalidDiceNotation as exc:
                return f"Invalid dice notation: {exc}"

            success = None if dc is None else total >= dc
            rolls_made.append(
                {"dice": notation, "total": total, "rolls": rolls, "dc": dc, "success": success, "reason": reason}
            )

            if dc is None:
                return f"Rolled {notation}: {total} {rolls}."
            return f"Rolled {notation}: {total} {rolls} vs DC {dc} — {'success' if success else 'failure'}."

        def apply_update(update: dict) -> str:
            nonlocal sheet_changed

            target = update.get("target") or "self"

            # A model given the character sheet as JSON (which includes its own
            # player_id and name) sometimes echoes one of those back as target
            # instead of "self" - without this, that misroutes into the NPC
            # branch below and silently creates a phantom NPC sheet named after
            # the player's own id or name.
            if target in ("self", player_id, character.name):
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

        def update_world(update: dict) -> str:
            nonlocal world_changed
            result = self._session.world.apply_update(update)
            if not result.startswith("No changes applied"):
                world_changed = True
            return result

        buffer = ""
        async for chunk in self._dm.narrate(
            history=self._session.history,
            character_summary=character.model_dump_json(),
            action_text=action_text,
            apply_update=apply_update,
            request_roll=request_roll,
            update_world=update_world,
        ):
            buffer += chunk
            await self._broadcast(self._log_envelope("narration", chunk, done=False))
        await self._broadcast(self._log_envelope("narration", "", done=True))

        for roll in rolls_made:
            await self._broadcast(self._log_envelope("dice", self._dice_log_text(character.name, roll)))
            await self._broadcast(self._dice_result_envelope(player_id, roll))

        if sheet_changed:
            await self._send_to(player_id, self._character_update_envelope(player_id, character))

        for name in npcs_touched:
            await self._broadcast(self._npc_update_envelope(name, self._session.npcs[name]))

        if world_changed:
            await self._broadcast(self._world_update_envelope())

        if (
            check_for_missed_changes
            and not sheet_changed
            and not npcs_touched
            and POSSIBLE_UNTRACKED_CHANGE_PATTERN.search(buffer)
        ):
            await self._send_to(
                player_id,
                self._system_envelope(
                    "The DM's narration may describe a change that wasn't recorded - "
                    "your sheet might be out of sync with the story.",
                    level="warning",
                ),
            )

        return buffer

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

        roll = {"dice": notation, "total": total, "rolls": rolls, "dc": None, "success": None, "reason": reason}
        await self._broadcast(self._log_envelope("dice", self._dice_log_text(name, roll)))
        await self._broadcast(self._dice_result_envelope(player_id, roll))

    @staticmethod
    def _dice_log_text(name: str, roll: dict) -> str:
        label = f" ({roll['reason']})" if roll["reason"] else ""
        text = f"{name} rolls {roll['dice']}{label}: {roll['total']} {roll['rolls']}"
        if roll["dc"] is not None:
            text += f" vs DC {roll['dc']}"
        if roll["success"] is not None:
            text += " — success" if roll["success"] else " — failure"
        return text

    def _dice_result_envelope(self, roller_id: str, roll: dict) -> Envelope:
        payload = {
            "roller_id": roller_id,
            "dice": roll["dice"],
            "result": roll["total"],
            "rolls": roll["rolls"],
            "purpose": roll["reason"],
        }
        if roll["dc"] is not None:
            payload["dc"] = roll["dc"]
            payload["success"] = roll["success"]
        return Envelope(
            type="dice_result", session_id=self._session.session_id, sender_id="server", payload=payload
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

    def _world_update_envelope(self) -> Envelope:
        # Broadcast, not private - world state (objectives, location, flags)
        # is shared observable fiction, same reasoning as _npc_update_envelope.
        return Envelope(
            type="world_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload=self._session.world.model_dump(),
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
