from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from shared.protocol import Envelope

from . import dice
from .narrator import NarratorBackend
from .persistence import SessionStore
from .rules import RulesIndex
from .state import CharacterSheet, Session

logger = logging.getLogger(__name__)

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]

# Mirrors _on_join_session's own hp=10, max_hp=10 fallback for a fresh
# player character - the safety net for an NPC introduced without a
# real max_hp from lookup_rule, not the intended path.
DEFAULT_NPC_HP = 10

# A real, deterministic starting sheet instead of every new character
# beginning as a blank name+HP-10 with nothing else - the immersion gap
# this closes: a fresh character sheet previously showed nothing but a
# name and HP, since stats/inventory otherwise only get populated if the
# DM's update_character tool happens to fire, which this project's whole
# reliability investigation (ROADMAP.md) has shown is unreliable. This
# stays deliberately small: a class picks starting HP (from the SRD's own
# hit_die, no ability-score/CON-modifier system - real 5e uses hit die +
# CON mod, this project has no ability scores at all yet) and a starting
# item or two from the SRD's existing (limited, CC-BY-4.0) equipment list.
# Not a full 5e character build - see ROADMAP.md for what's deliberately
# left for later (ability scores, more classes/equipment).
CLASS_STARTING_EQUIPMENT: dict[str, list[str]] = {
    "fighter": ["Longsword", "Leather Armor"],
    "rogue": ["Shortbow", "Leather Armor"],
    "cleric": ["Leather Armor", "Potion of Healing"],
    "wizard": ["Potion of Healing"],
}


def _public_character_view(character: CharacterSheet) -> dict:
    """The subset of a player character's sheet visible to *other* players -
    name, class, HP, and conditions, but never inventory/stats/notes. Backs
    every other-player-facing broadcast (player_joined, player_update, and
    a non-owning recipient's own entry in state_sync's characters dict) so
    there's exactly one place defining what's public - matches the same
    "others shouldn't see your inventory" boundary character_update's
    owner-only routing already established (docs/protocol.md)."""
    return {
        "player_id": character.player_id,
        "name": character.name,
        "character_class": character.character_class,
        "hp": character.hp,
        "max_hp": character.max_hp,
        "conditions": list(character.conditions),
    }


def _hit_die_max(hit_die: str) -> int:
    # SRD hit_die values are like "d10" - the max roll, used here as this
    # project's level-1 HP (no ability scores yet to add a CON modifier).
    return int(hit_die.lstrip("d"))


def build_starting_character(
    player_id: str, name: str, character_class: str, rules: RulesIndex
) -> CharacterSheet:
    """Builds a real starting sheet from a chosen class via the SRD data,
    or falls back to the original blank hp=10/max_hp=10 sheet for a blank
    or unrecognized class - keeps old clients/tests that don't send
    character_class at all working unchanged."""
    class_entry = rules.get_entry("class", character_class) if character_class else None
    if class_entry is None:
        return CharacterSheet(player_id=player_id, name=name, hp=10, max_hp=10)

    max_hp = _hit_die_max(class_entry["hit_die"])
    return CharacterSheet(
        player_id=player_id,
        name=name,
        hp=max_hp,
        max_hp=max_hp,
        character_class=class_entry["name"],
        inventory=list(CLASS_STARTING_EQUIPMENT.get(character_class.strip().lower(), [])),
    )

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
    r"kills?|killed|unconscious|collapses?|hp|health|"
    # Condition language was the stated intent above ("damage/death/
    # condition") but never actually made it into the pattern - a real
    # gap, not a hypothetical one: live-reproduced 2026-08-07 (see
    # ROADMAP.md), a combat turn narrated a leaked `add_condition:
    # "frozen"` pseudo-tool-call ("chilling your skin", "numbing cold")
    # with no real tool call, and this heuristic stayed silent on it.
    r"condition|poison(?:ed|ing)?|stun(?:s|ned|ning)?|paraly(?:zed|zing|sis)|"
    r"frozen|freez(?:e|es|ing)|chill(?:s|ed|ing)?|numb(?:s|ed|ing)?|"
    r"blind(?:ed|ing)?|burn(?:s|ed|ing)?|prone|restrained)\b",
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
        rules: RulesIndex | None = None,
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store
        self._enable_opening_scene = enable_opening_scene
        self._rules = rules or RulesIndex.load_default()

    async def _save(self, notify_player_id: str | None = None) -> None:
        """Persists session state - best-effort, not fatal. Previously a
        save failure propagated uncaught from whichever _on_* handler
        called it, silently killing that connection with nothing shown to
        the player - the exact incident logged in ROADMAP.md (a directory
        that vanished mid-process-life turned every _save() into an
        unhandled FileNotFoundError). Catches and warns instead, the same
        "report, don't block the turn" pattern _narrate_and_apply's own
        failure handling already uses. Narrowed to OSError deliberately -
        the realistic failure class here (missing directory, disk full,
        permissions changing mid-run), not a catch-all that would also
        mask a genuine bug in what's being serialized."""
        if self._store is None:
            return
        try:
            self._store.save(self._session)
        except OSError:
            logger.exception("Failed to save session %s", self._session.session_id)
            if notify_player_id is not None:
                await self._send_to(
                    notify_player_id,
                    self._system_envelope(
                        "Your progress may not be saving right now - see the server log.", level="warning"
                    ),
                )

    async def handle(self, envelope: Envelope) -> None:
        handler = getattr(self, f"_on_{envelope.type}", None)
        if handler is not None:
            await handler(envelope)

    async def _on_join_session(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        is_new_character = player_id not in self._session.characters

        if is_new_character:
            name = envelope.payload.get("player_name", player_id)
            character_class = envelope.payload.get("character_class", "")
            self._session.characters[player_id] = build_starting_character(
                player_id, name, character_class, self._rules
            )
            self._session.turn_order.append(player_id)
            await self._save(player_id)

        character = self._session.characters[player_id]
        await self._send_to(player_id, self._state_sync_envelope(player_id))
        await self._broadcast(self._system_envelope(f"{character.name} joined the session.", level="info"))
        # Structured counterpart to the text log line above - lets a client
        # add/refresh this player's presence line (left-column "other
        # players" view) without parsing prose. Fires on every join,
        # including a reconnect, so a client's own local roster stays
        # correct even after missing an earlier player_left.
        await self._broadcast(self._player_joined_envelope(character))

        # Narration and turn-taking only become visible once the adventure
        # has actually started (see _on_start_session below) - a fresh join
        # lands in the client's pre-game lobby, not mid-turn-prompt. This
        # join is a genuine reconnect into an already-started game - where
        # the returning player should still see whose turn it is - iff
        # _has_started() says so.
        if self._has_started() and self._session.current_turn == player_id:
            await self._broadcast(self._turn_prompt_envelope())

    def _has_started(self) -> bool:
        # Session.started is the authoritative signal going forward, but a
        # session saved before that field existed would load as False even
        # with real narration history already in it - bool(log) is the
        # fallback that keeps an old real save (this project's own
        # sessions/*.json among others) correctly recognized as already
        # started rather than getting dropped back into a pre-game lobby.
        return self._session.started or bool(self._session.log)

    async def _on_start_session(self, envelope: Envelope) -> None:
        """The lobby's "Start Adventure" trigger - any joined player may
        send this (Oracle has no separate host/GM role - turn order and
        now session-starting are both symmetric across players). Decoupled
        from _on_join_session on purpose: joining creates your character
        and lets you review it/chat in the lobby, but the DM doesn't
        narrate and the turn queue doesn't become visible until someone
        explicitly starts things - see docs/protocol.md.

        Idempotent via _has_started(): a second start_session (another
        player also clicking Start around the same moment, or a retry after
        the first one narrated fine) after the adventure has already begun
        is a silent no-op, not a re-narrated opening scene. Session.started
        is set True unconditionally the moment this actually proceeds - not
        only after a successful narration - so a failed/disabled opening
        scene doesn't leave the session re-triggerable on every future
        start_session; see _narrate_opening_scene's own best-effort framing
        for why a failure there still shouldn't undo this."""
        if self._has_started() or not self._session.characters:
            return

        self._session.started = True
        await self._save(envelope.sender_id)

        player_id = envelope.sender_id
        character = self._session.characters.get(player_id) or next(iter(self._session.characters.values()))

        roster = list(self._session.characters.values())
        if len(roster) > 1:
            # A group opening scene isn't a new multi-actor tool-routing
            # mechanism (character_summary/apply_update still anchor on one
            # character, same as any other turn) - just a richer prompt so
            # the DM's narration acknowledges everyone actually present
            # instead of assuming a lone traveler. Owner's own framing was
            # "maybe begin with players introducing themselves" - a nudge,
            # not a hard requirement, so this stays a prompt-level note.
            names = ", ".join(
                f"{c.name} the {c.character_class}" if c.character_class else c.name for c in roster
            )
            action_text = (
                f"(The adventure begins. Players present: {names}. Set an opening scene that draws "
                "everyone in together - consider inviting them to introduce themselves.)"
            )
        else:
            action_text = "(The adventure begins - set an opening scene to draw the player in.)"

        # session_started fires BEFORE narration, not after - a real
        # ordering bug caught before it ever shipped: _narrate_and_apply
        # (inside _narrate_opening_scene) broadcasts log_entry narration
        # chunks and any npc_update as it streams, and a client still on
        # the lobby screen has nowhere to render them yet. Broadcasting
        # session_started first lets every client transition into the real
        # session view first, then watch the opening scene stream in live -
        # the same experience a normal turn's narration already gives.
        await self._broadcast(self._session_started_envelope())

        if self._enable_opening_scene:
            await self._narrate_opening_scene(character, action_text)

        if self._session.current_turn is not None:
            await self._broadcast(self._turn_prompt_envelope())

    async def handle_disconnect(self, player_id: str) -> None:
        """Called by the transport when a connected player's socket closes -
        the counterpart to the player_joined broadcast above, so everyone
        else's presence view drops them. Not routed through handle()/
        envelope dispatch since a disconnect isn't a client-sent event -
        the transport is the only thing that actually observes it."""
        character = self._session.characters.get(player_id)
        name = character.name if character else player_id
        await self._broadcast(self._player_left_envelope(player_id, name))

    async def _narrate_opening_scene(self, character: CharacterSheet, action_text: str) -> None:
        """Best-effort: a failed opening scene shouldn't leave the lobby
        stuck, so failures here are reported but don't propagate like a
        real turn's would. Reuses the exact same narrate()/tool-wiring path
        a real turn uses, via a synthetic action_text (built by the caller,
        _on_start_session - see there for why it varies with roster size),
        so the DM can set an initial location/objective with update_world
        exactly like any other turn. check_for_missed_changes=False: an
        opening scene routinely sets a scene using words this heuristic
        watches for (a village recently attacked, a wounded NPC met in
        passing) with no mechanical change ever expected on turn zero - a
        real false-positive class, not a hypothetical one."""
        try:
            buffer = await self._narrate_and_apply(character, action_text, check_for_missed_changes=False)
        except Exception:
            logger.exception("Opening scene narration failed for player_id=%s", character.player_id)
            await self._send_to(
                character.player_id,
                self._system_envelope("Couldn't generate an opening scene.", level="warning"),
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.append_turn("(The adventure begins.)", buffer)
        await self._save(character.player_id)

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
            logger.exception("Turn narration failed for player_id=%s", player_id)
            await self._send_to(
                player_id, self._system_envelope(f"The DM couldn't respond: {exc}", level="error")
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.append_turn(text, buffer)
        self._session.advance_turn()
        await self._save(player_id)
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
                total, rolls, sides = dice.roll(notation)
            except dice.InvalidDiceNotation as exc:
                return f"Invalid dice notation: {exc}"

            success = None if dc is None else total >= dc
            rolls_made.append(
                {
                    "dice": notation, "total": total, "rolls": rolls, "sides": sides,
                    "dc": dc, "success": success, "reason": reason,
                }
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

            # Keyed by a casefolded form of the name, not the raw target
            # string - an inconsistently-cased target from the DM (e.g.
            # "Bandit" one turn, "bandit" the next) would otherwise silently
            # create a second, disconnected NPC entry instead of updating the
            # one already being tracked. npc.name keeps the first-seen
            # casing for display, so the tool result and broadcasts stay
            # consistent turn to turn regardless of how later calls case it.
            npc_key = target.casefold()
            npc = self._session.npcs.get(npc_key)
            introduced = npc is None

            if introduced:
                # Same default-HP fallback join_session already uses for a
                # fresh player character - a safety net for when the DM
                # forgets to pass a real max_hp from lookup_rule, not the
                # intended path.
                max_hp = update.get("max_hp") or DEFAULT_NPC_HP
                npc = CharacterSheet(player_id=target, name=target, hp=max_hp, max_hp=max_hp)
                self._session.npcs[npc_key] = npc

            delta_result = npc.apply_update(update)
            changed = not delta_result.startswith("No changes applied")

            # Introducing a new NPC is itself a real change worth
            # broadcasting even if this same call's deltas were a no-op
            # (e.g. just naming it with no damage yet) - matches the
            # player-character path's own "only broadcast on a real
            # change" rule otherwise.
            if introduced or changed:
                npcs_touched.add(npc_key)

            if introduced:
                intro = f"Introduced {npc.name} (HP {npc.hp}/{npc.max_hp})."
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
            # The private character_update above carries the full sheet
            # (inventory included) to the owner; everyone else's presence
            # view needs the same public-only fields player_joined already
            # established, kept live rather than only ever set at join time.
            await self._broadcast(self._player_update_envelope(character))

        for npc_key in npcs_touched:
            touched_npc = self._session.npcs[npc_key]
            await self._broadcast(self._npc_update_envelope(touched_npc.name, touched_npc))

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
            total, rolls, sides = dice.roll(notation)
        except dice.InvalidDiceNotation as exc:
            await self._send_to(player_id, self._system_envelope(str(exc), level="warning"))
            return

        roll = {
            "dice": notation, "total": total, "rolls": rolls, "sides": sides,
            "dc": None, "success": None, "reason": reason,
        }
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
            "sides": roll["sides"],
            "purpose": roll["reason"],
        }
        if roll["dc"] is not None:
            payload["dc"] = roll["dc"]
            payload["success"] = roll["success"]
        return Envelope(
            type="dice_result", session_id=self._session.session_id, sender_id="server", payload=payload
        )

    def _state_sync_envelope(self, recipient_id: str) -> Envelope:
        return Envelope(
            type="state_sync",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                # The recipient's own entry is a full sheet (inventory
                # included); every other player's entry is the same public
                # view player_joined/player_update broadcast - a (re)joining
                # player shouldn't see everyone else's inventory just
                # because it's bundled into their own sync.
                "characters": {
                    pid: (c.model_dump() if pid == recipient_id else _public_character_view(c))
                    for pid, c in self._session.characters.items()
                },
                # Keyed by each NPC's own stored (first-seen-casing) name for
                # display, not the internal casefolded dict key - keeps a
                # reconnecting client's status lines consistent with what
                # npc_update broadcasts already show.
                "npcs": {npc.name: npc.model_dump() for npc in self._session.npcs.values()},
                "world_state": self._session.world.model_dump(),
                "turn_order": self._session.turn_order,
                "current_turn": self._session.current_turn,
                "log_tail": self._session.log[-20:],
                # _has_started(), not the raw field - so a client can route
                # correctly (lobby vs. session view) even for the disabled-
                # or failed-narration case where log stays empty despite the
                # adventure genuinely having started (see _has_started()).
                "started": self._has_started(),
            },
        )

    def _character_update_envelope(self, player_id: str, character: CharacterSheet) -> Envelope:
        return Envelope(
            type="character_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "sheet_delta": character.model_dump()},
        )

    def _player_joined_envelope(self, character: CharacterSheet) -> Envelope:
        # Broadcast (everyone, including the joining player themselves - same
        # as the system_envelope "X joined the session" line already is),
        # public-view-only payload, same boundary _state_sync_envelope's
        # non-owning entries already establish.
        return Envelope(
            type="player_joined",
            session_id=self._session.session_id,
            sender_id="server",
            payload=_public_character_view(character),
        )

    def _player_left_envelope(self, player_id: str, name: str) -> Envelope:
        return Envelope(
            type="player_left",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "name": name},
        )

    def _session_started_envelope(self) -> Envelope:
        # Broadcast, empty payload - a pure lifecycle signal telling every
        # client still in the pre-game lobby to transition into the real
        # session view. Deliberately not inferred from the first narration
        # log_entry arriving (fragile, and narration is best-effort - see
        # _narrate_opening_scene - so it might not arrive at all); this
        # fires unconditionally once _on_start_session has genuinely
        # transitioned the session out of "not yet started".
        return Envelope(
            type="session_started",
            session_id=self._session.session_id,
            sender_id="server",
            payload={},
        )

    def _player_update_envelope(self, character: CharacterSheet) -> Envelope:
        # The public counterpart to _character_update_envelope's private,
        # full-sheet push - keeps every other player's presence view (HP,
        # conditions) live turn to turn without ever including inventory.
        return Envelope(
            type="player_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload=_public_character_view(character),
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
