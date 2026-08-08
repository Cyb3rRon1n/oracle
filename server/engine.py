from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from shared.protocol import Envelope

from . import dice
from .narrator import NarratorBackend
from .persistence import SessionStore
from .rules import RulesIndex
from .state import ABILITY_KEYS, CharacterSheet, Session, ability_modifier

logger = logging.getLogger(__name__)

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]

# Mirrors _on_join_session's own hp=10, max_hp=10 fallback for a fresh
# player character - the safety net for an NPC introduced without a
# real max_hp from lookup_rule, not the intended path.
DEFAULT_NPC_HP = 10

# Fallback XP for defeating an NPC whose name doesn't match a known SRD
# monster (see _xp_for_npc below) and whose introduction didn't carry an
# explicit "xp" override - CR 1/4's real SRD value (server/rules/srd.json's
# xp_by_cr), the same tier every monster currently in the SRD subset except
# the orc actually sits at, so this is a reasonable floor rather than an
# arbitrary round number.
DEFAULT_NPC_XP = 50

# Conditions that impose disadvantage on the *bearer's own* rolls, per
# their real SRD description (server/rules/srd.json's own condition text)
# - a real, deliberate subset of the five conditions this project tracks,
# not all of them. grappled has no self-roll effect at all in the real
# text (only a speed-0 movement effect, and Oracle has no speed/movement
# system to hook that to) - correctly excluded, not a gap. stunned's real
# effects (incapacitated, auto-fail STR/DEX saves, attacks *against* it
# have advantage) are a structurally different kind of mechanic -
# target-side and turn-blocking, not "the bearer rolls worse" - and
# deliberately not modeled in this slice; a stunned character can still
# act and rolls normally here, a real known gap, not silently pretended
# away. Also a broader simplification than strict RAW: poisoned/
# frightened's real text excludes saving throws specifically, but
# request_roll has no way to distinguish a save from a check from an
# attack roll today, so this applies disadvantage to *any* roll while the
# condition is active rather than narrowly scoped per roll type.
DISADVANTAGE_CONDITIONS = frozenset({"poisoned", "frightened", "prone"})

# character_edit's own real scope (docs/protocol.md, ROADMAP.md's "let a
# player edit their own notes/inventory directly, without DM adjudication"):
# deliberately just the fields that are pure player-side bookkeeping, not
# mechanical state. hp/conditions/stats/xp/ac all stay DM- or engine-only -
# the same "the engine or the DM decides mechanical state, the player only
# decides fiction/bookkeeping" boundary update_character's own tool schema
# already draws, just enforced from the other direction here.
CHARACTER_EDIT_FIELDS = frozenset({"notes", "add_item", "remove_item"})


def _has_disadvantage(character: CharacterSheet) -> list[str]:
    """Returns which of the acting character's current conditions actually
    trigger disadvantage (empty if none) - a list, not just a bool, so a
    caller can name the real reason in the roll's own text rather than a
    bare "disadvantage" with no explanation. Real 5e disadvantage never
    stacks (multiple sources still just apply once) - callers only need
    `bool(...)` on this to know whether to roll with disadvantage at all,
    the list itself is purely for the human-readable reason."""
    return [c for c in character.conditions if c.casefold() in DISADVANTAGE_CONDITIONS]


# A real, deterministic starting sheet instead of every new character
# beginning as a blank name+HP-10 with nothing else - the immersion gap
# this closes: a fresh character sheet previously showed nothing but a
# name and HP, since stats/inventory otherwise only get populated if the
# DM's update_character tool happens to fire, which this project's whole
# reliability investigation (ROADMAP.md) has shown is unreliable. This
# stays deliberately small: a class picks starting HP (the SRD's own
# hit_die max, plus a real CON modifier - see _generate_stats below) and a
# starting item or two from the SRD's existing (limited, CC-BY-4.0)
# equipment list. Not a full 5e character build - see ROADMAP.md for
# what's still deliberately left for later (more classes/equipment,
# player-chosen stat allocation instead of a fixed per-class array).
CLASS_STARTING_EQUIPMENT: dict[str, list[str]] = {
    "fighter": ["Longsword", "Leather Armor"],
    "rogue": ["Shortbow", "Leather Armor"],
    "cleric": ["Leather Armor", "Potion of Healing"],
    "wizard": ["Potion of Healing"],
}

# The SRD's own real Standard Array (Basic Rules character-creation
# option), not an invented spread - same "use the official SRD numbers,
# don't make one up" convention this file's XP-per-CR/XP-per-level tables
# already follow.
STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

# Deliberately hand-written per class, not derived from a formula - same
# style CLASS_STARTING_EQUIPMENT already uses, and grounded in real data
# already in this dataset rather than a fresh judgment call: each class's
# own two entries are exactly its SRD saving_throws (server/rules/srd.json
# - e.g. fighter's "Strength, Constitution"), CON placed second for every
# class as a universal survival stat, and the remaining three ordered by
# ordinary class-archetype priority (a caster wants its remaining physical
# stat over its remaining mental one, etc.). A blank/unrecognized class
# has no entry here and gets no stats at all - the same fallback
# build_starting_character's HP/inventory already use.
CLASS_ABILITY_PRIORITY: dict[str, tuple[str, ...]] = {
    "fighter": ("str", "con", "dex", "wis", "cha", "int"),
    "wizard": ("int", "con", "dex", "wis", "cha", "str"),
    "rogue": ("dex", "con", "int", "wis", "cha", "str"),
    "cleric": ("wis", "con", "str", "dex", "cha", "int"),
}


def _generate_stats(character_class: str) -> dict[str, int]:
    """Assigns the SRD's real Standard Array to a class's own ability
    priority order - deterministic (the same class always gets the same
    array), matching this project's existing "no ability-score system
    should depend on chance" stance nowhere written down but implied by
    every other deterministic mechanic here (XP awards, level-1 HP).
    Player-chosen stat allocation is real future work, not attempted here
    - see ROADMAP.md."""
    priority = CLASS_ABILITY_PRIORITY.get(character_class.strip().lower())
    if priority is None:
        return {}
    return dict(zip(priority, STANDARD_ARRAY))


def _armor_base_ac(equipment_entry: dict) -> int | None:
    """Parses the base AC number out of a real SRD equipment entry's own
    `ac` field (e.g. "11 + Dex modifier") - `None` for equipment with no
    `ac` field at all (most equipment isn't armor) or a shape this can't
    parse, so a caller can fall back rather than guess a value."""
    ac_text = equipment_entry.get("ac")
    if not ac_text:
        return None
    match = re.match(r"(\d+)", ac_text)
    return int(match.group(1)) if match else None


def _compute_ac(inventory: list[str], dex_modifier: int, rules: RulesIndex) -> int:
    """Real 5e's own formula: 10 (unarmored) + DEX modifier, or an equipped
    armor's own base AC + DEX modifier if better. Generalized over whatever
    armor actually appears in `inventory` (matched against real SRD
    equipment data) rather than hardcoded to one item, so it keeps working
    without a code change if more armor is added to srd.json later - only
    leather_armor exists there today, and it happens to have no DEX cap,
    so a capped-armor-type case (medium/heavy armor in real 5e) is real,
    untested future work, not something this formula already handles."""
    base = 10
    for item in inventory:
        entry = rules.get_entry("equipment", item)
        if entry is not None:
            armor_base = _armor_base_ac(entry)
            if armor_base is not None:
                base = max(base, armor_base)
    return base + dex_modifier


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
        "ac": character.ac,
        "conditions": list(character.conditions),
        # Whether a character is actively dying or has died is exactly the
        # kind of urgent, visible-to-the-table fact HP/conditions already
        # are - other players need to know "Rowan is dying!" to have any
        # chance of reacting to it. The raw death_save_successes/failures
        # counts stay owner-only (below, full model_dump() only) - real
        # bookkeeping toward the outcome, the same private/public split
        # xp/level already draws.
        "dying": character.dying,
        "dead": character.dead,
        # level, not xp - level is a meaningful public fact about a
        # character (like class or HP), the same way another player's
        # level is visible on their sheet at a real table. xp itself stays
        # owner-only (the full model_dump() in _state_sync_envelope/
        # _character_update_envelope), matching the existing inventory/
        # stats/notes privacy boundary - raw XP is bookkeeping, not
        # something other players need to see turn to turn.
        "level": character.level,
    }


def _hit_die_max(hit_die: str) -> int:
    # SRD hit_die values are like "d10" - the max roll, not an actual per-
    # level roll (real 5e typically rolls past level 1); callers add the
    # character's real CON modifier on top of this (see
    # build_starting_character and the level-up HP growth in apply_update
    # below) - the max-roll-only simplification is what's left, not the
    # missing CON modifier this comment used to flag.
    return int(hit_die.lstrip("d"))


def _xp_for_npc(npc: CharacterSheet, update: dict, rules: RulesIndex) -> int:
    """Decides how much XP defeating this NPC is worth, in priority order:
    (1) an explicit "xp" in the killing update - the same override pattern
    max_hp already has for introducing an NPC, lets the DM hand-tune a
    boss or a trivial mook without touching the SRD data; (2) the NPC's own
    name matched against the SRD's monster list (the same _slug()-based
    lookup RulesIndex.get_entry() already does for narration/`lookup_rule`)
    and its "cr" field run through xp_for_cr - free and automatic whenever
    an NPC happens to be named after a known monster (the "goblin"/"orc"/
    etc. targets this project's own existing NPC tests already use); (3)
    DEFAULT_NPC_XP, the same "not the intended path, just a safety net"
    role DEFAULT_NPC_HP already plays for max_hp."""
    explicit = update.get("xp")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit

    monster_entry = rules.get_entry("monster", npc.name)
    if monster_entry is not None:
        cr_xp = rules.xp_for_cr(monster_entry.get("cr", ""))
        if cr_xp is not None:
            return cr_xp

    return DEFAULT_NPC_XP


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

    stats = _generate_stats(character_class)
    con_mod = ability_modifier(stats["con"]) if stats else 0
    # Real 5e's level-1 HP formula: hit die max + CON modifier, floored at
    # 1 (a character can't start with 0 or negative HP even from a bad
    # CON score) - the CON-modifier half of the "no ability-score/CON
    # system yet" gap this file used to flag is closed by this line.
    max_hp = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod)
    inventory = list(CLASS_STARTING_EQUIPMENT.get(character_class.strip().lower(), []))
    dex_mod = ability_modifier(stats["dex"]) if stats else 0
    return CharacterSheet(
        player_id=player_id,
        name=name,
        hp=max_hp,
        max_hp=max_hp,
        character_class=class_entry["name"],
        stats=stats,
        inventory=inventory,
        ac=_compute_ac(inventory, dex_mod, rules),
    )


def _character_from_import(player_id: str, imported: dict) -> CharacterSheet | None:
    """Builds a CharacterSheet from a client-submitted export file
    (join_session's optional imported_character field - client/app.py's
    WelcomeScreen/export_character). The exported dict is just a prior
    session's own full CharacterSheet.model_dump(), so this is mostly a
    pass-through - but player_id is always overridden to the real joining
    connection's id, never trusted from the file itself (a stale or
    tampered export shouldn't let one connection claim another's already-
    tracked identity). Any other shape mismatch (a hand-edited or
    corrupted file, or one from some future/incompatible sheet version) is
    caught and treated as "no import" rather than a crash - the caller
    falls back to a fresh build_starting_character() sheet, the same
    graceful-fallback convention that function's own blank/unrecognized-
    class handling already established. The client does its own lighter
    read/JSON-parse validation first (_load_character_file), but this is
    the real trust boundary - a client is never authoritative for another
    connection's data, so the shape gets fully re-validated here too."""
    try:
        return CharacterSheet(**{**imported, "player_id": player_id})
    except (ValidationError, TypeError):
        return None

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

            character = None
            imported = envelope.payload.get("imported_character")
            if imported is not None:
                character = _character_from_import(player_id, imported)
                if character is None:
                    await self._send_to(
                        player_id,
                        self._system_envelope(
                            "Couldn't import that character file - starting fresh instead.",
                            level="warning",
                        ),
                    )
            if character is None:
                character = build_starting_character(player_id, name, character_class, self._rules)

            self._session.characters[player_id] = character
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

        # An unconscious character (hp == 0, whichever of the three real
        # states that covers - actively dying, stabilized-but-unconscious,
        # or dead) can't take a normal action at all under real 5e - the
        # same "It's not your turn" rejection pattern just above, applied
        # to a different reason a submitted action can't proceed. Doesn't
        # advance the turn (return, not a fallthrough), so a dying player
        # keeps getting reprompted until they resolve via /deathsave
        # (exempt from turn order, like /roll - see _on_death_save) rather
        # than the turn silently skipping past them.
        if character.hp == 0:
            if character.dead:
                message = f"{character.name} has died and can't act."
            elif character.dying:
                message = f"{character.name} is unconscious and dying - use /deathsave, not a normal action."
            else:
                message = f"{character.name} is unconscious at 0 HP and needs healing before acting again."
            await self._send_to(player_id, self._system_envelope(message, level="warning"))
            return

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
        # Captured before this turn's apply_update closure can mutate them
        # (the acting character is the same mutable object throughout this
        # call), the same "compare before vs. after" pattern was_alive/
        # defeated already use for NPC XP below - so a real transition into
        # or out of dying can be announced after narration resolves, not
        # just silently reflected in the next character_update/player_update.
        was_dying = character.dying
        was_dead = character.dead
        sheet_changed = False
        npcs_touched: set[str] = set()
        rolls_made: list[dict] = []
        world_changed = False
        # (npc_name, xp_awarded, levels_gained) - one entry per NPC this
        # turn's apply_update calls actually defeated, so a broadcast can
        # announce each defeat/level-up after narration finishes streaming,
        # not interrupt it mid-stream.
        xp_awards: list[tuple[str, int, int]] = []

        def request_roll(update: dict) -> str:
            notation = update.get("dice", "1d20")
            dc = update.get("dc")
            reason = update.get("reason", "")

            # weapon, when given, is an equipment name (e.g. "longsword") -
            # the engine looks up its real SRD damage die and uses that as
            # the notation instead of whatever the model typed, the same
            # "resolve real data server-side rather than trust the model to
            # get it right" reasoning ability already applies. A name that
            # doesn't match known equipment falls through to the given
            # dice unchanged - the same graceful-miss convention every
            # other name-based lookup in this file already follows.
            weapon = update.get("weapon")
            damage_type = None
            if weapon:
                equipment_entry = self._rules.get_entry("equipment", weapon)
                weapon_damage = equipment_entry.get("damage") if equipment_entry else None
                if weapon_damage:
                    notation, _, damage_type = weapon_damage.partition(" ")

            # ability, when given, is the acting character's own ability
            # key (e.g. "dex") - the engine looks up its real modifier
            # (CharacterSheet.stat_modifiers, already precomputed) and adds
            # it itself, rather than trusting the DM to compute
            # floor((score-10)/2) correctly and splice it into the dice
            # string by hand. dice.roll()'s notation regex only supports
            # one signed modifier group anyway (no "1d20+3+2"), so this
            # also sidesteps a real parsing limitation, not just a
            # reliability one. Composes naturally with weapon above - a
            # real 5e damage roll is exactly "weapon's die + ability mod".
            ability = update.get("ability")
            ability_mod = character.stat_modifiers.get(ability) if ability else None

            # Fully automatic, never a model-supplied field - the same
            # "the engine computes this from real tracked state, not the
            # model's judgment" reasoning every other mechanic in this
            # file already follows. A character narrating their way into
            # a disadvantageous circumstance the engine has no tracked
            # state for (fighting in darkness, an ally in the way) still
            # isn't modeled - real, deliberate future work, not silently
            # promised here.
            disadvantage_reasons = _has_disadvantage(character)
            disadvantage = bool(disadvantage_reasons)

            try:
                total, rolls, sides = dice.roll(
                    notation, extra_modifier=ability_mod or 0, disadvantage=disadvantage
                )
            except dice.InvalidDiceNotation as exc:
                return f"Invalid dice notation: {exc}"

            success = None if dc is None else total >= dc
            rolls_made.append(
                {
                    "dice": notation, "total": total, "rolls": rolls, "sides": sides,
                    "dc": dc, "success": success, "reason": reason,
                    "ability": ability, "ability_modifier": ability_mod,
                    "damage_type": damage_type,
                    "disadvantage": disadvantage, "disadvantage_reasons": disadvantage_reasons,
                }
            )

            damage_label = f" ({damage_type})" if damage_type else ""
            ability_label = f" +{ability_mod} {ability.upper()}" if ability_mod is not None else ""
            disadvantage_label = f" (disadvantage: {', '.join(disadvantage_reasons)})" if disadvantage else ""
            label = damage_label + ability_label + disadvantage_label
            if dc is None:
                return f"Rolled {notation}{label}: {total} {rolls}."
            return f"Rolled {notation}{label}: {total} {rolls} vs DC {dc} — {'success' if success else 'failure'}."

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
                # A known SRD monster's real ability scores were already
                # sitting in srd.json, just never connected to a tracked
                # NPC before - the same target-name lookup _xp_for_npc uses
                # for CR, applied here too so a DM introducing e.g. a real
                # "goblin" gets its actual stat block (and therefore real
                # modifiers on any request_roll targeting it) for free,
                # not just for player characters.
                monster_entry = self._rules.get_entry("monster", target)
                if monster_entry is not None:
                    npc.stats = dict(monster_entry.get("stats", {}))
                    # A real 5e monster's AC is a flat authored value (armor,
                    # natural hide, etc. already folded in) - copied
                    # directly, unlike a player's AC which is *computed*
                    # from armor + DEX (_compute_ac above). Falls back to
                    # CharacterSheet.ac's own default (10) if this monster
                    # entry has no "ac" field, same as an unmatched name.
                    if "ac" in monster_entry:
                        npc.ac = monster_entry["ac"]
                self._session.npcs[npc_key] = npc

            # Captured before apply_update mutates hp - this is the
            # deterministic trigger for XP, not a tool the DM has to
            # remember to call. ROADMAP.md's reliability investigation
            # found tool-call reliability plateaus around 29% across every
            # local model tested, so awarding XP off "the model also called
            # an award_xp tool" would silently fail most of the time; hp
            # crossing from >0 to 0 is already-observed, already-reliable
            # engine state (this same apply_update path is what the
            # untracked-change heuristic above exists to catch failures
            # of). was_alive on a freshly-introduced NPC is True unless it
            # was introduced already-dead in the same update (max_hp<=0),
            # which correctly awards no XP for a "corpse" that was never
            # alive in this session.
            was_alive = npc.hp > 0

            delta_result = npc.apply_update(update)
            changed = not delta_result.startswith("No changes applied")
            defeated = was_alive and npc.hp == 0

            # Introducing a new NPC is itself a real change worth
            # broadcasting even if this same call's deltas were a no-op
            # (e.g. just naming it with no damage yet) - matches the
            # player-character path's own "only broadcast on a real
            # change" rule otherwise.
            if introduced or changed:
                npcs_touched.add(npc_key)

            xp_note = ""
            if defeated:
                # XP goes to whoever's turn it is, not split across the
                # whole party - a deliberate simplifying default (real 5e
                # splits party-wide), chosen because Oracle's turn queue
                # already anchors every mechanical update on a single
                # acting character (apply_update/request_roll/update_world
                # all take just one `character`) - party-wide XP would need
                # a session-wide "who else is present" notion this turn
                # loop doesn't have. Revisit if/when a real multi-character-
                # per-turn scenario shows up.
                xp_award = _xp_for_npc(npc, update, self._rules)
                levels_gained = character.gain_xp(xp_award, self._rules.xp_thresholds())
                if levels_gained:
                    class_entry = (
                        self._rules.get_entry("class", character.character_class)
                        if character.character_class else None
                    )
                    if class_entry is not None:
                        # Same real formula as level-1 HP (hit die max +
                        # CON modifier, floored at 1 per level) - a
                        # character with a negative CON modifier still
                        # gains at least 1 HP per level, never 0 or
                        # negative growth.
                        con_mod = ability_modifier(character.stats["con"]) if character.stats else 0
                        hp_gain = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod) * levels_gained
                        character.max_hp += hp_gain
                        character.hp += hp_gain
                sheet_changed = True
                xp_awards.append((npc.name, xp_award, levels_gained))
                xp_note = f" {npc.name} is defeated! {character.name} gains {xp_award} XP."
                if levels_gained:
                    xp_note += f" {character.name} reaches level {character.level}!"

            if introduced:
                intro = f"Introduced {npc.name} (HP {npc.hp}/{npc.max_hp})."
                result = f"{intro} {delta_result}" if changed else intro
            else:
                result = delta_result
            return result + xp_note

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

        # Broadcast once narration/sheet/npc updates have all gone out, so
        # this reads as the resolution of what just streamed rather than
        # interrupting it. system_message rather than a new envelope type -
        # matches how every other game-flow announcement not itself DM
        # narration (a join, an out-of-turn refusal) already reaches
        # clients, so no client-side changes were needed to render this.
        for npc_name, xp_award, levels_gained in xp_awards:
            text = f"{character.name} defeats {npc_name} and gains {xp_award} XP!"
            if levels_gained:
                text += f" {character.name} reaches level {character.level}!"
            await self._broadcast(self._system_envelope(text, level="info"))

        # A dying/dead transition is already reflected in the sheet_changed
        # character_update/player_update broadcasts above, but neither of
        # those reads as an announcement the way the XP-award text just
        # above does - a player watching HP tick to 0 in a redacted "Party"
        # view shouldn't have to notice that themselves. Compared against
        # was_dying/was_dead captured before narration started, not just
        # "is dying now", so a character who was already dying before this
        # turn (e.g. from the automatic damage-while-down failure inside
        # CharacterSheet.apply_update) doesn't get re-announced every turn.
        if character.dead and not was_dead:
            await self._broadcast(self._system_envelope(f"{character.name} has died.", level="warning"))
        elif character.dying and not was_dying:
            await self._broadcast(
                self._system_envelope(
                    f"{character.name} drops to 0 HP and is dying! Roll a death save with /deathsave.",
                    level="warning",
                )
            )
        elif was_dying and not character.dying and not character.dead:
            await self._broadcast(self._system_envelope(f"{character.name} is no longer dying.", level="info"))

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
                    advisory=True,
                ),
            )

        return buffer

    async def _on_chat_message(self, envelope: Envelope) -> None:
        await self._broadcast(self._log_envelope("chat", envelope.payload.get("text", "")))

    async def _on_character_edit(self, envelope: Envelope) -> None:
        """Player-side bookkeeping - notes, or adding/removing an inventory
        item by name - that doesn't need DM adjudication (docs/protocol.md).
        Deliberately the mirror image of apply_update's mechanical fields:
        this handler only ever touches notes/inventory, never hp/conditions/
        stats/xp, so a player editing their own sheet can't grant themselves
        healing or gear out of nowhere the DM never narrated. Exempt from
        turn order like chat_message/dice_roll - only _on_player_action
        checks current_turn."""
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        if character is None:
            await self._send_to(
                player_id, self._system_envelope("You don't have a character to edit yet.", level="warning")
            )
            return

        field = envelope.payload.get("field")
        value = envelope.payload.get("value")
        if field not in CHARACTER_EDIT_FIELDS or not value:
            await self._send_to(
                player_id,
                self._system_envelope(f"Can't edit '{field}' - try notes, add_item, or remove_item.", level="warning"),
            )
            return

        if field == "notes":
            character.notes = str(value)
        elif field == "add_item":
            character.inventory.append(str(value))
        elif field == "remove_item":
            item = str(value)
            if item not in character.inventory:
                await self._send_to(
                    player_id, self._system_envelope(f"You don't have '{item}' to remove.", level="warning")
                )
                return
            character.inventory.remove(item)

        # Private only, the same boundary _public_character_view draws -
        # notes/inventory never appear in player_update/player_joined, so
        # unlike a mechanical sheet_changed update (_narrate_and_apply) there's
        # no public counterpart broadcast to send here.
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        await self._save(player_id)

    async def _on_death_save(self, envelope: Envelope) -> None:
        """A dying player's own roll against death (docs/protocol.md's
        "Death saves" section) - deliberately its own dedicated event, not
        folded into dice_roll. A death save is always a fixed 1d20 with no
        notation for a player to choose, and needs outcome bookkeeping
        (successes/failures/stabilize/died) no other roll has to carry -
        reusing dice_roll's free-text notation input would mean either a
        player has to remember to always type "/roll 1d20" or this handler
        special-cases dice_roll internally anyway, neither simpler than a
        dedicated event.

        Exempt from turn order, like dice_roll/character_edit - deliberately
        not tied to "the start of the dying character's own turn" the way
        real 5e's rule actually works. Automating that would mean hooking
        into turn advancement/turn_prompt to roll for a dying player
        automatically and skip their turn for them - a bigger, riskier
        change to the core turn loop than this slice needs, so it's a real,
        named simplification rather than a silently-dropped nuance: a
        player can /deathsave whenever they like, not just once per their
        own turn, and nothing here enforces a once-per-turn cap."""
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        if character is None:
            await self._send_to(player_id, self._system_envelope("You don't have a character yet.", level="warning"))
            return
        if character.dead:
            await self._send_to(
                player_id, self._system_envelope(f"{character.name} has already died.", level="warning")
            )
            return
        if not character.dying:
            await self._send_to(
                player_id, self._system_envelope("You're not making death saves right now.", level="warning")
            )
            return

        total, rolls, sides = dice.roll("1d20")
        natural = rolls[0]

        # A natural 20 is real 5e's own special case, resolved before the
        # normal success/failure bookkeeping below rather than folded into
        # it: the character doesn't just log a success, they regain 1 HP
        # and wake back up immediately, ending the dying state outright
        # regardless of however many successes/failures had already
        # accumulated.
        if natural == 20:
            character.hp = 1
            character.dying = False
            character.death_save_successes = 0
            character.death_save_failures = 0
            outcome_text = f"{character.name} claws back to consciousness with 1 HP!"
        elif natural == 1:
            # Counts as two failures under real 5e's own rule -
            # record_death_save's count=2 stops early if the second
            # failure would be redundant (already dead from the first).
            outcome_text = character.record_death_save(success=False, count=2)
        elif natural >= 10:
            outcome_text = character.record_death_save(success=True)
        else:
            outcome_text = character.record_death_save(success=False)

        # dc=10 (real 5e's own death-save threshold) reuses dice_result's
        # existing dc/success rendering wholesale - the client already
        # shows "vs DC 10 — success/failure" and highlights a natural 20/1
        # on any roll, so a death save needed zero new client-side
        # rendering code for the roll itself, only the outcome text below.
        roll = {
            "dice": "1d20", "total": total, "rolls": rolls, "sides": sides,
            "dc": 10, "success": total >= 10, "reason": "death save",
            "disadvantage": False, "disadvantage_reasons": [],
        }
        await self._broadcast(self._log_envelope("dice", f"{character.name} rolls a death save: {total}."))
        await self._broadcast(self._dice_result_envelope(player_id, roll))
        await self._broadcast(
            self._system_envelope(outcome_text, level="warning" if character.dead else "info")
        )
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        await self._broadcast(self._player_update_envelope(character))
        await self._save(player_id)

    async def _on_dice_roll(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        name = character.name if character else player_id
        notation = envelope.payload.get("dice", "")
        reason = envelope.payload.get("reason", "")

        # A manually-typed /roll is real state too, not exempt from a
        # tracked condition just because the DM didn't request it - the
        # same automatic, deterministic disadvantage request_roll's own
        # closure applies.
        disadvantage_reasons = _has_disadvantage(character) if character else []
        disadvantage = bool(disadvantage_reasons)

        try:
            total, rolls, sides = dice.roll(notation, disadvantage=disadvantage)
        except dice.InvalidDiceNotation as exc:
            await self._send_to(player_id, self._system_envelope(str(exc), level="warning"))
            return

        roll = {
            "dice": notation, "total": total, "rolls": rolls, "sides": sides,
            "dc": None, "success": None, "reason": reason,
            "disadvantage": disadvantage, "disadvantage_reasons": disadvantage_reasons,
        }
        await self._broadcast(self._log_envelope("dice", self._dice_log_text(name, roll)))
        await self._broadcast(self._dice_result_envelope(player_id, roll))

    @staticmethod
    def _dice_log_text(name: str, roll: dict) -> str:
        # ability/ability_modifier are only ever present on a DM-requested
        # roll (request_roll) - a plain player /roll never sets them, so
        # .get() rather than indexing keeps this one shared helper working
        # for both call sites without every dict-building call site having
        # to carry two always-None keys just for this function's benefit.
        ability_mod = roll.get("ability_modifier")
        ability_label = f" +{ability_mod} {roll['ability'].upper()}" if ability_mod is not None else ""
        damage_type = roll.get("damage_type")
        damage_label = f" ({damage_type})" if damage_type else ""
        disadvantage_reasons = roll.get("disadvantage_reasons")
        disadvantage_label = f" (disadvantage: {', '.join(disadvantage_reasons)})" if disadvantage_reasons else ""
        label = f" ({roll['reason']})" if roll["reason"] else ""
        text = (
            f"{name} rolls {roll['dice']}{damage_label}{ability_label}{disadvantage_label}{label}: "
            f"{roll['total']} {roll['rolls']}"
        )
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
        if roll.get("ability_modifier") is not None:
            payload["ability"] = roll["ability"]
            payload["ability_modifier"] = roll["ability_modifier"]
        if roll.get("damage_type"):
            payload["damage_type"] = roll["damage_type"]
        if roll.get("disadvantage"):
            payload["disadvantage"] = True
            payload["disadvantage_reasons"] = roll["disadvantage_reasons"]
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

    def _system_envelope(self, text: str, level: str = "info", advisory: bool = False) -> Envelope:
        # advisory is deliberately narrow - only the missed-change heuristic
        # (below) sets it. Every other system_message (connection/turn-order/
        # save-failure) is a plain fact about what just happened; this one is
        # a "you might want to double check" nudge the player should weigh,
        # not act on unconditionally - a real, different category from an
        # ordinary warning, even though both currently share level="warning".
        # Only included when true, the same "don't carry an always-False
        # field" convention dice_result's own optional fields already follow.
        payload: dict = {"level": level, "text": text}
        if advisory:
            payload["advisory"] = True
        return Envelope(
            type="system_message",
            session_id=self._session.session_id,
            sender_id="server",
            payload=payload,
        )
