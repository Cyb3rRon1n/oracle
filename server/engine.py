from __future__ import annotations

import base64
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from shared.protocol import Envelope

from . import dice
from .character_build import (
    CLASS_SAVING_THROW_PROFICIENCIES,
    CLASS_SKILL_PROFICIENCIES,
    _apply_ability_score_improvements,
    _asi_announcement,
    _character_from_import,
    _hit_die_max,
    build_starting_character,
)
from .lore import (
    OriginTable,
    WorldBible,
    load_default_origin_table,
    load_default_world_bible,
)
from .image_backend import ImageBackend
from .lorebook import MAX_LORE_CHARS, SUPPORTED_SUFFIXES, Lorebook
from .narrator import NarratorBackend
from .persistence import SessionStore
from .portrait import DEFAULT_STYLE, build_portrait_prompt
from .rolls import (
    DEFAULT_NPC_HP,
    DEFAULT_NPC_XP,  # noqa: F401 - re-exported for tests
    _cast_spell,
    _compute_ac,
    _dice_roll_tags,
    _equip_slot_for,
    _has_disadvantage,
    _use_item,
    _xp_for_npc,
    is_weapon_proficient,  # noqa: F401 - re-exported for tests
)
from .rules import RulesIndex
from .state import (
    SKILL_ABILITIES,
    SPELLCASTING_ABILITY,
    CharacterSheet,
    Session,
    ability_modifier,
)
from .views import (
    _attack_lines,  # noqa: F401 - re-exported for tests
    _npc_roster,
    _outcome_category,
    _owner_character_view,
    _public_character_view,
)

logger = logging.getLogger(__name__)

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]

# Resolved turns between campaign-summary rebuilds (window holds ~6).
CAMPAIGN_SUMMARY_INTERVAL = 10

# Tavern-keeper (lobby only): the shortest gap between keeper lines. Deliberately
# long - a keeper that answers every message is the research anti-pattern.
KEEPER_MIN_INTERVAL = 20.0

# Fact-ledger injection: newest facts always reach the DM; older ones only
# when one of their 5+ char words appears in the current action/location.
LEDGER_RECENT_LIMIT = 12
LEDGER_RELEVANT_LIMIT = 8

# What a player may set on their own sheet via character_edit: pure
# fiction/bookkeeping only, plus consuming something already owned.
# hp/conditions/stats/xp stay DM- or engine-only, with one narrow, deliberate
# exception: use_item's healing only ever consumes an item the DM already
# granted (via update_character's own add_item), never fabricates a new
# resource - a player can't grant themselves gear this way, only spend what
# they're holding. equip/unequip change AC as a side effect but the player
# only names an owned item, never a number (_compute_ac does the rest). The
# DM reads the RP text fields via character_summary but never writes them.
# There is deliberately no player-initiated add_item: an item only ever
# enters inventory through update_character's own add_item (the DM's tool),
# never a player's own free-text request.
CHARACTER_EDIT_TEXT_FIELDS = frozenset({"notes", "personality", "ideals", "bonds", "flaws", "alignment"})
CHARACTER_EDIT_FIELDS = CHARACTER_EDIT_TEXT_FIELDS | frozenset(
    {"remove_item", "equip", "unequip", "use_item", "cast_spell"}
)

# Session-zero tone choice, prepended to every turn's action_text while
# active (per-session, so it can't live in the shared system prompt).
# "standard" has no entry - WorldBible.tone_guidance already covers it.
CONTENT_PREFERENCE_HINTS = {
    "lighter": (
        "Session tone: keep this lighter - ease off graphic violence, gore, and dark or "
        "traumatic themes. Prefer non-graphic outcomes and a more hopeful, adventurous feel."
    ),
    "intense": (
        "Session tone: don't hold back - real danger, real stakes, and darker themes are "
        "welcome here, described with real weight rather than softened."
    ),
}


def _sanitize_suggested_actions(raw: list) -> list[dict]:
    """Coerces the DM's own suggested_actions into a real, validated shape -
    {text, skill?, dc?} - never trusted strictly from model output. A plain
    string (either backend's older behavior, or a model that ignores the
    object shape) becomes {text: <that string>}. An unrecognized skill name
    is dropped rather than shown as a fabricated check - the same
    graceful-miss convention every other skill lookup here already
    follows. dc is only kept alongside a real skill; a dc with no
    recognized skill isn't a check the client can meaningfully label."""
    result = []
    for item in raw[:4]:
        if isinstance(item, str):
            if item:
                result.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not text:
            continue
        entry = {"text": str(text)}
        skill = item.get("skill")
        if isinstance(skill, str) and skill.lower() in SKILL_ABILITIES:
            entry["skill"] = skill.lower()
            dc = item.get("dc")
            if isinstance(dc, int) and not isinstance(dc, bool):
                entry["dc"] = dc
        result.append(entry)
    return result


def _party_xp_announcement(npc_name: str, xp_award: int, party_results: list[tuple]) -> str:
    """Builds the player-facing defeat/level-up broadcast text for a kill,
    shared by the in-turn apply_update closure and the player-confirmed
    correction path so the two can't drift - the same reason _asi_announcement
    exists. A solo session (one member) keeps the original single-actor
    phrasing; a party kill names the per-member share and every member who
    leveled."""
    if len(party_results) == 1:
        _pid, member, levels_gained, asi_abilities = party_results[0]
        text = f"{member.name} defeats {npc_name} and gains {xp_award} XP!"
        if levels_gained:
            text += f" {member.name} reaches level {member.level}!"
        text += _asi_announcement(member.name, asi_abilities)
        return text
    share = xp_award // len(party_results)
    text = f"The party defeats {npc_name} and gains {xp_award} XP ({share} each)!"
    for _pid, member, levels_gained, asi_abilities in party_results:
        if levels_gained:
            text += f" {member.name} reaches level {member.level}!"
        text += _asi_announcement(member.name, asi_abilities)
    return text


# Narration wording that suggests an unrecorded mechanical change - flags a
# possibly-stale sheet for the player, not a verdict. Deliberately narrow;
# check_missed_change() is the real second channel.
POSSIBLE_UNTRACKED_CHANGE_PATTERN = re.compile(
    r"\b(damage|wound(?:s|ed|ing)?|bleed(?:s|ing)?|dies?|dead|death|slain|"
    r"kills?|killed|unconscious|collapses?|hp|health|"
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
        world_bible: WorldBible | None = None,
        origin_table: OriginTable | None = None,
        image_backend: ImageBackend | None = None,
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store
        # None is a normal, supported state (see image_backend.py's
        # create_image_backend) - portrait generation is optional, unlike
        # the DM backend above, which is always required.
        self._image_backend = image_backend
        self._enable_opening_scene = enable_opening_scene
        self._rules = rules or RulesIndex.load_default()
        # Composes the opening scene's premise with real setting facts.
        self._world_bible = world_bible or load_default_world_bible()
        # Feeds build_starting_character's random per-character origin.
        self._origin_table = origin_table or load_default_origin_table()
        self._pending_proposals: dict[str, dict] = {}
        # player_ids who armed their held Inspiration; consumed by the next d20
        # roll in request_roll. Engine-local, not persisted - re-arm on reconnect.
        self._inspiration_armed: set[str] = set()
        # Players with at least one live connection. handle_disconnect fires
        # only on a player's last connection, so this is multi-tab safe.
        self._connected_players: set[str] = set()
        # Rate-limit clock for the lobby tavern-keeper. monotonic seconds of the
        # last keeper line; 0 = never spoken. Engine-local, resets with the lobby.
        self._last_keeper_ts: float = 0.0
        # World-context lorebook (docs/protocol.md): empty until context_select;
        # rebuilt from disk on every selection change and on load.
        self._world_context_dir = Path(os.environ.get("WORLD_CONTEXT_DIR", "world_context"))
        self._lorebook = Lorebook()
        if session.context_files:
            self._rebuild_lorebook(session.context_files)
        # Legacy migration: a session saved before `started` existed loads with
        # started=False despite real narration history. adventures_completed
        # distinguishes that from a party legitimately back in the tavern.
        if session.log and not session.started and session.adventures_completed == 0:
            session.started = True

    def _manifest_files(self) -> list[dict]:
        """The world_context/ directory listing - names/types/sizes only,
        no content (docs/protocol.md). A missing directory is an empty
        manifest, not an error: the feature simply has nothing to offer."""
        if not self._world_context_dir.is_dir():
            return []
        files = [
            {
                "name": p.name,
                "type": p.suffix.lstrip(".").lower(),
                "size_chars": len(p.read_text(encoding="utf-8", errors="replace")),
            }
            for p in sorted(self._world_context_dir.iterdir())
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        return files

    def _rebuild_lorebook(self, names: list[str]) -> None:
        known = {f["name"] for f in self._manifest_files()}
        safe_names = [n for n in names if n in known]
        paths = [self._world_context_dir / n for n in safe_names]
        self._lorebook = Lorebook.from_files(paths)

    async def _on_context_manifest_request(self, envelope: Envelope) -> None:
        await self._send_to(
            envelope.sender_id,
            Envelope(
                type="context_manifest",
                session_id=self._session.session_id,
                sender_id="server",
                payload={"files": self._manifest_files(), "selected": self._session.context_files},
            ),
        )

    async def _on_context_select(self, envelope: Envelope) -> None:
        names = envelope.payload.get("files")
        if not isinstance(names, list):
            await self._send_to(
                envelope.sender_id,
                self._system_envelope("Invalid world-context selection.", level="error"),
            )
            return
        known = {f["name"] for f in self._manifest_files()}
        selected = [n for n in names if isinstance(n, str) and n in known]
        dropped = [n for n in names if n not in selected]
        self._session.context_files = selected
        self._rebuild_lorebook(selected)
        oversized = [
            e.title or "(untitled)"
            for e in self._lorebook.entries
            if len(e.injection_text()) + 32 > MAX_LORE_CHARS
        ]
        if dropped:
            await self._send_to(
                envelope.sender_id,
                self._system_envelope(
                    "Ignored unknown world-context file(s): " + ", ".join(map(str, dropped)),
                    level="warning",
                ),
            )
        if oversized:
            await self._send_to(
                envelope.sender_id,
                self._system_envelope(
                    "Too large to ever inject under the lore budget: "
                    + ", ".join(oversized)
                    + ". Split it into smaller sections.",
                    level="warning",
                ),
            )
        await self._save(envelope.sender_id)

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

    def _apply_level_up(self, character: CharacterSheet, levels_gained: int, old_level: int) -> list[str]:
        """Shared by the in-turn NPC-defeat path (apply_update's closure)
        and the player-confirmed correction path (_on_apply_proposed_change,
        below) - the post-XP level-up math is identical for both. Returns
        the ASI abilities applied (empty when nothing gained)."""
        class_entry = (
            self._rules.get_entry("class", character.character_class)
            if character.character_class else None
        )
        if class_entry is not None:
            # HP gain per level: hit die max + CON modifier, floored at 1 per
            # level - pure SRD, no cushion (that's a one-time level-1 add, see
            # STARTING_HP_CUSHION). A character with a negative CON modifier
            # still gains at least 1 HP per level, never 0 or negative growth.
            con_mod = ability_modifier(character.stats["con"]) if character.stats else 0
            hp_gain = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod) * levels_gained
            character.max_hp += hp_gain
            character.hp += hp_gain
            # Hit-dice pool tracks level; the new dice arrive unspent.
            character.hit_die = class_entry["hit_die"]
            character.hit_dice_total = character.level
            character.hit_dice_remaining += levels_gained
            # ponytail: AC doesn't recompute when a post-creation ASI changes DEX.
            asi_abilities = _apply_ability_score_improvements(character, old_level, character.level)
            # Slots grow by the old->new max delta, not a reset - a level-up
            # grants more, it isn't a free rest. Non-caster: new_max is {}, no-op.
            if character.max_spell_slots or character.known_spells:
                new_max = self._rules.spell_slots_by_level(character.level)
                for slot_level, count in new_max.items():
                    gained = count - character.max_spell_slots.get(slot_level, 0)
                    if gained > 0:
                        character.spell_slots[slot_level] = character.spell_slots.get(slot_level, 0) + gained
                character.max_spell_slots = new_max
            return asi_abilities
        return []

    def _award_party_xp(self, xp_award: int) -> list[tuple[str, CharacterSheet, int, list[str]]]:
        """Splits a kill's XP party-wide (5e's rule), deterministically. Floor
        division, remainder drops; solo session awards it all to the one member.
        Returns (player_id, member, levels_gained, asi_abilities) per member.
        Shared by apply_update's closure and _on_apply_proposed_change."""
        members = list(self._session.characters.values())
        if not members:
            return []
        share = xp_award // len(members)
        results = []
        for member in members:
            old_level = member.level
            levels_gained = member.gain_xp(share, self._rules.xp_thresholds())
            asi_abilities = self._apply_level_up(member, levels_gained, old_level)
            results.append((member.player_id, member, levels_gained, asi_abilities))
        return results

    async def handle(self, envelope: Envelope) -> None:
        handler = getattr(self, f"_on_{envelope.type}", None)
        if handler is not None:
            await handler(envelope)

    async def _on_join_session(self, envelope: Envelope) -> None:
        player_id = envelope.sender_id
        is_new_character = player_id not in self._session.characters
        self._connected_players.add(player_id)

        if is_new_character:
            name = envelope.payload.get("player_name", player_id)
            character_class = envelope.payload.get("character_class", "")
            race = envelope.payload.get("race", "")
            # Optional override of the class's default ability-priority order
            # (see _generate_stats), which re-validates and falls back.
            raw_stat_priority = envelope.payload.get("stat_priority")
            stat_priority = (
                tuple(raw_stat_priority)
                if isinstance(raw_stat_priority, list) and all(isinstance(a, str) for a in raw_stat_priority)
                else None
            )

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
                character = build_starting_character(
                    player_id, name, character_class, self._rules, self._origin_table, stat_priority, race
                )
                # A typo'd class string otherwise falls back to a blank,
                # stat-less character silently. Only warns when the player
                # actually typed something - a blank field is the UI's own
                # "blank to skip" option.
                if character_class.strip() and not character.character_class:
                    await self._send_to(
                        player_id,
                        self._system_envelope(
                            f"'{character_class.strip()}' isn't a recognized class - starting without one "
                            "(no starting stats, HP bonus, or kit). Recognized classes: fighter, wizard, "
                            "rogue, cleric.",
                            level="warning",
                        ),
                    )
                # Same for a typo'd race string (costs the ability bonus and
                # racial traits otherwise).
                if race.strip() and not character.race:
                    await self._send_to(
                        player_id,
                        self._system_envelope(
                            f"'{race.strip()}' isn't a recognized race - starting without one "
                            "(no ability bonus or racial traits). Recognized races: human, elf, dwarf, "
                            "halfling.",
                            level="warning",
                        ),
                    )

            self._session.characters[player_id] = character
            self._session.turn_order.append(player_id)
            await self._save(player_id)
        else:
            # A reconnect keeps the existing character's name/class whatever
            # was typed on the welcome screen. Heads-up only when the typed
            # name genuinely differs from what they're playing.
            typed_name = envelope.payload.get("player_name")
            existing_character = self._session.characters[player_id]
            if typed_name and typed_name != existing_character.name:
                await self._send_to(
                    player_id,
                    self._system_envelope(
                        f"Welcome back - you're reconnecting as your existing character "
                        f"'{existing_character.name}', not a new '{typed_name}'.",
                        level="info",
                    ),
                )

        character = self._session.characters[player_id]
        await self._send_to(player_id, self._state_sync_envelope(player_id))

        # A private "story so far" recap for anyone joining an already-started
        # session (returning or brand-new). Deterministic, not an LLM call.
        # Must go out AFTER state_sync: the client only leaves WelcomeScreen
        # once it processes state_sync, so an earlier system_message is dropped.
        if self._has_started():
            await self._send_to(player_id, self._system_envelope(self._resume_recap(), level="info"))
            # A returning character also gets that grounding fed to the DM
            # once, on their next action (see Session.pending_dm_recap).
            if not is_new_character and player_id not in self._session.pending_dm_recap:
                self._session.pending_dm_recap.append(player_id)

        await self._broadcast(self._system_envelope(f"{character.name} joined the session.", level="info"))
        # Structured counterpart to the log line above - a client refreshes
        # this player's presence line without parsing prose. Fires on reconnect
        # too, so a roster that missed an earlier player_left still corrects.
        await self._broadcast(self._player_joined_envelope(character))

        if not self._has_started():
            await self._maybe_keeper_line("greeting")

        # Turn-taking is only visible once the adventure has started; a reconnect
        # into a started game should still see whose turn it is.
        if self._has_started():
            # Rescue a queue left pointing at a player who is long gone.
            if await self._skip_absent_players() or self._session.current_turn == player_id:
                await self._broadcast(self._turn_prompt_envelope())

    def _has_started(self) -> bool:
        # Plain flag now: the legacy "log but no `started`" saves are migrated
        # to started=True in __init__, so False here genuinely means the lobby -
        # a fresh session, or a party back in the tavern between adventures.
        return self._session.started

    def _resume_recap(self) -> str:
        """Composes _on_join_session's private "story so far" recap, sent
        to anyone (returning or brand-new) joining an already-started
        session - see the call site's own comment for why this is
        deterministic rather than an LLM call. Falls back gracefully
        through three tiers of "what do
        we actually know": WorldState.summary (only ever set by
        update_world, Anthropic-only/opt-in today - see ROADMAP.md item 6
        - so frequently empty), then the most recent real narration line
        in the log (always available once the story has genuinely begun),
        then a bare acknowledgement if even that's somehow missing (should
        be unreachable given _has_started() already guards this being
        called at all, but a graceful floor rather than an IndexError)."""
        world = self._session.world
        parts = ["Welcome back."]

        if world.summary:
            parts.append(world.summary)
        else:
            last_narration = next(
                (
                    entry.get("text", "")
                    for entry in reversed(self._session.log)
                    if entry.get("kind") == "narration" and entry.get("text")
                ),
                "",
            )
            if last_narration:
                snippet = (
                    last_narration
                    if len(last_narration) <= 240
                    else last_narration[:240].rsplit(" ", 1)[0] + "..."
                )
                parts.append(f"Last thing that happened: {snippet}")

        if world.location and world.location != "unknown":
            parts.append(f"You're currently at {world.location}.")

        active_objectives = [o.text for o in world.objectives if o.status == "active"]
        if active_objectives:
            parts.append("Active objectives: " + "; ".join(active_objectives) + ".")

        return " ".join(parts)

    def _seed_world_map(self) -> str:
        """Seed the campaign map with the world bible's known regions at
        session start (docs/protocol.md "Map"), so the Map tab isn't an empty
        canvas. Only bible regions with coordinates seed nodes; edges derive
        from border text naming a sibling region. Returns the apply_update
        summary ('' when nothing seeded)."""
        placed = [r for r in self._world_bible.regions if r.x is not None and r.y is not None]
        if not placed:
            return ""
        summary = self._session.world.apply_update({
            "map_nodes": [{"name": r.name, "x": r.x, "y": r.y} for r in placed],
        })
        by_name = {r.name: r for r in placed}
        edges: set[tuple[str, str]] = set()
        for region in placed:
            borders_text = " ".join(region.borders).casefold()
            for name in by_name:
                if name != region.name and name.casefold() in borders_text:
                    edges.add(tuple(sorted((region.name, name))))
        for a, b in sorted(edges):
            self._session.world.apply_update({"connect_locations": [a, b]})
        return summary

    async def _on_start_session(self, envelope: Envelope) -> None:
        """The lobby's "Start Adventure" trigger - any joined player may send
        this (Oracle has no host/GM role). Idempotent via _has_started().
        Session.started is set True the moment this proceeds, not only after a
        successful narration, so a failed opening scene isn't re-triggerable."""
        if self._has_started() or not self._session.characters:
            return

        # Unrecognized/missing falls back to the "standard" default rather
        # than raising on a malformed payload.
        content_preference = envelope.payload.get("content_preference")
        if content_preference in ("lighter", "standard", "intense"):
            self._session.content_preference = content_preference

        self._session.started = True
        self._session.ready_players.clear()
        chosen_hook = self._selected_hook()
        self._session.quest_hooks.clear()
        self._session.hook_votes.clear()
        if self._seed_world_map():
            # Push the seeded map now - otherwise it only reaches a client on
            # its next full state_sync.
            await self._broadcast(self._world_update_envelope())
        await self._save(envelope.sender_id)

        player_id = envelope.sender_id
        character = self._session.characters.get(player_id) or next(iter(self._session.characters.values()))

        roster = list(self._session.characters.values())
        names = ", ".join(
            f"{c.name} the {c.character_class}" if c.character_class else c.name for c in roster
        )
        if self._session.adventures_completed > 0:
            # A later adventure: the party's already in this world - no
            # near-death/Guardian beat, just setting out again from where they
            # rested. (The quest-board hook will thread in here.)
            action_text = self._world_bible.next_adventure_prompt(
                names, self._session.world.location, self._session.campaign_summary, chosen_hook
            )
        elif len(roster) > 1:
            # A richer prompt so the opening narration acknowledges everyone
            # present; tool-routing still anchors on one character.
            action_text = self._world_bible.opening_scene_prompt(names, plural=True)
        else:
            action_text = self._world_bible.opening_scene_prompt(
                character.name, plural=False, origin_detail=character.background
            )

        # session_started must go out BEFORE narration: _narrate_opening_scene
        # streams log_entry chunks, and a client still on the lobby screen has
        # nowhere to render them.
        await self._broadcast(self._session_started_envelope())

        if self._enable_opening_scene:
            await self._narrate_opening_scene(character, action_text)

        if self._session.current_turn is not None:
            # Even the first turn skips a player who's already stepped away.
            await self._skip_absent_players()
            await self._broadcast(self._turn_prompt_envelope())

    async def _on_start_combat(self, envelope: Envelope) -> None:
        """Real 5e formal initiative - any joined player may trigger this,
        explicitly (the DM model isn't trusted to reliably notice "combat
        started"). Idempotent: a second start_combat while in combat is a no-op.

        Rolls a real 1d20 + DEX modifier for every present player and every
        currently-tracked NPC (server/dice.py's roll(), the same primitive
        every other roll in this project already uses) - Oracle now has
        real DEX modifiers for both (Ability scores; NPCs matched against a
        known SRD monster get real stats on introduction), so this needed
        no new data. An NPC/character with no stats at all (a blank/
        unrecognized class, or an NPC that never matched a known monster)
        gets a modifier of 0, the same fallback every other stat-dependent
        mechanic here already uses rather than a special case.

        Deliberately scoped: NPCs are announced in the rolled order (so the
        table knows when they act relative to the players) but never enter
        `turn_order` itself - only player ids do. Giving NPCs real
        mechanical turn slots would need a genuinely new engine mechanism
        (an autonomous "it's the goblin's turn" step with no player input
        at all, nothing like it exists anywhere in this project today) -
        deliberately deferred rather than guessed at; see ROADMAP.md."""
        if self._session.in_combat or not self._session.characters:
            return

        participants: list[tuple[str, int, int, str | None]] = []  # (name, roll, dex_mod, player_id)
        for player_id, character in self._session.characters.items():
            dex_mod = character.stat_modifiers.get("dex", 0)
            total, _, _ = dice.roll("1d20", extra_modifier=dex_mod)
            participants.append((character.name, total, dex_mod, player_id))
        for npc in self._session.npcs.values():
            dex_mod = npc.stat_modifiers.get("dex", 0)
            total, _, _ = dice.roll("1d20", extra_modifier=dex_mod)
            participants.append((npc.name, total, dex_mod, None))

        # Highest roll first, ties to higher DEX modifier (5e's own tiebreak),
        # then stable-sort join order.
        participants.sort(key=lambda p: (-p[1], -p[2]))

        self._session.pre_combat_turn_order = list(self._session.turn_order)
        self._session.turn_order = [pid for (_, _, _, pid) in participants if pid is not None]
        self._session.current_turn_index = 0
        self._session.in_combat = True
        await self._skip_absent_players()

        order_text = ", ".join(f"{name} ({roll})" for name, roll, _, _ in participants)
        await self._broadcast(self._system_envelope(f"Combat begins! Initiative order: {order_text}.", level="info"))
        await self._broadcast(self._turn_prompt_envelope())
        await self._save()

    async def _on_end_combat(self, envelope: Envelope) -> None:
        """Any joined player may end combat. Idempotent outside combat.
        Restores turn_order to the pre-combat snapshot plus any mid-combat
        joiners, in join order."""
        if not self._session.in_combat:
            return

        pre_combat = self._session.pre_combat_turn_order or []
        latecomers = [pid for pid in self._session.turn_order if pid not in pre_combat]
        self._session.turn_order = pre_combat + latecomers
        self._session.pre_combat_turn_order = None
        self._session.current_turn_index = 0
        self._session.in_combat = False
        await self._skip_absent_players()

        await self._broadcast(self._system_envelope("Combat ends.", level="info"))
        if self._session.current_turn is not None:
            await self._broadcast(self._turn_prompt_envelope())
        await self._save()

    async def _on_end_adventure(self, envelope: Envelope) -> None:
        """Any joined player may wrap up the current adventure and take the
        party back to the tavern lobby - the counterpart to start_session.
        A no-op if not started. The durable campaign summary is refreshed
        first (so a short adventure isn't forgotten), then the rolling history
        window is cleared for the next arc. Party, world location, completed
        objectives and the log all persist; the ready-check resets."""
        session = self._session
        if not session.started:
            return

        await self._refresh_campaign_summary()
        session.history.clear()
        session.turns_since_summary = 0
        session.started = False
        session.adventures_completed += 1
        session.ready_players.clear()
        session.in_combat = False
        session.pre_combat_turn_order = None
        session.current_turn_index = 0
        session.quest_hooks.clear()
        session.hook_votes.clear()
        self._last_keeper_ts = 0.0

        await self._broadcast(self._session_ended_envelope())
        # Refresh the roster so the lobby's ready dots reflect the cleared check.
        for character in session.characters.values():
            await self._broadcast(self._player_update_envelope(character))
        await self._broadcast(self._system_envelope(
            "The adventure winds down. The party makes its way back to the tavern.", level="info"
        ))
        await self._save(envelope.sender_id)

    async def _on_request_quests(self, envelope: Envelope) -> None:
        """Quest board (docs/protocol.md "The tavern lobby"): the DM posts a few
        "what next" hooks the party votes on. Lobby-only, and only once the
        party has a story - a fresh session just starts cold. Generates on the
        first ask (or with `regenerate` for the "new hooks" button), which
        clears any votes; otherwise just re-broadcasts the current board."""
        session = self._session
        if self._has_started():
            return
        regenerate = bool(envelope.payload.get("regenerate"))
        if session.adventures_completed > 0 and (regenerate or not session.quest_hooks):
            gen = getattr(self._dm, "quest_hooks", None)
            hooks: list[str] = []
            if gen is not None:
                try:
                    hooks = await gen({
                        "party": ", ".join(
                            f"{c.name} the {c.character_class}".strip() for c in session.characters.values()
                        ),
                        "campaign_summary": session.campaign_summary or session.world.summary,
                        "location": session.world.location if session.world.location != "unknown" else "",
                        "completed_objectives": [
                            o.text for o in session.world.objectives if o.status == "completed"
                        ],
                    })
                except Exception:
                    logger.exception("Quest-hook generation failed for session %s", session.session_id)
            session.quest_hooks = hooks
            session.hook_votes = {}
            await self._save(envelope.sender_id)
        await self._broadcast(self._quest_board_envelope())

    async def _on_vote_quest(self, envelope: Envelope) -> None:
        """Toggle the sender's interest in one hook. A player backs at most one
        hook at a time - voting a second clears the first; voting the same one
        again clears it."""
        session = self._session
        if self._has_started():
            return
        hook = envelope.payload.get("hook")
        if hook not in session.quest_hooks:
            return
        pid = envelope.sender_id
        already = pid in session.hook_votes.get(hook, [])
        for voters in session.hook_votes.values():
            if pid in voters:
                voters.remove(pid)
        if not already:
            session.hook_votes.setdefault(hook, []).append(pid)
        await self._broadcast(self._quest_board_envelope())
        await self._save(pid)

    def _selected_hook(self) -> str:
        """The hook with a clear plurality of votes, or "" on none/a tie."""
        counts = {h: len(v) for h, v in self._session.hook_votes.items() if v}
        if not counts:
            return ""
        top = max(counts.values())
        leaders = [h for h, c in counts.items() if c == top]
        return leaders[0] if len(leaders) == 1 else ""

    async def _skip_absent_players(self) -> bool:
        """Advances the turn past any player with no live connection, so the
        round-robin queue never stalls on someone who isn't at the table.
        Bounded to one full cycle. Returns whether the turn moved."""
        if not self._has_started() or self._session.current_turn in self._connected_players:
            return False
        for _ in range(len(self._session.turn_order)):
            self._session.advance_turn()
            if self._session.current_turn in self._connected_players:
                return True
        return False

    async def handle_disconnect(self, player_id: str) -> None:
        """Called by the transport when a connected player's socket closes -
        the counterpart to the player_joined broadcast above, so everyone
        else's presence view drops them. Not routed through handle()/
        envelope dispatch since a disconnect isn't a client-sent event -
        the transport is the only thing that actually observes it. Fires
        only when the player's *last* connection closes, so closing one of
        several tabs doesn't count as leaving.

        Also moves the turn along when the departing player was the active
        one, announced once here in the moment it happens - see
        _skip_absent_players for the silent equivalent when the queue
        merely reaches someone who's already gone."""
        self._connected_players.discard(player_id)
        character = self._session.characters.get(player_id)
        name = character.name if character else player_id
        await self._broadcast(self._player_left_envelope(player_id, name))

        # In the lobby: the leaver drops their ready state (a reconnect
        # re-readies), and if everyone still connected is ready, the wait is over
        # - a party of two where one readies and the other just leaves still starts.
        if not self._has_started():
            if player_id in self._session.ready_players:
                self._session.ready_players.remove(player_id)
            connected = self._connected_players & self._session.characters.keys()
            if connected and connected <= set(self._session.ready_players):
                await self._on_start_session(Envelope(
                    type="start_session", session_id=self._session.session_id,
                    sender_id=next(iter(connected)), payload={},
                ))
                return

        if self._session.current_turn == player_id and await self._skip_absent_players():
            await self._broadcast(self._system_envelope(f"{name} is away - the turn passes on.", level="info"))
            await self._broadcast(self._turn_prompt_envelope())
            await self._save()

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

        # hp == 0 (dying / stabilized / dead) can't act. Doesn't advance the
        # turn, so a dying player keeps getting reprompted until /deathsave.
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
        self._pending_proposals.pop(player_id, None)
        await self._broadcast(self._log_envelope("action", f"{character.name}: {text}"))

        # Popped, not just read - the recap grounds only this first action
        # after reconnect, not every later turn.
        dm_recap = None
        if player_id in self._session.pending_dm_recap:
            self._session.pending_dm_recap.remove(player_id)
            dm_recap = self._resume_recap()

        try:
            buffer = await self._narrate_and_apply(character, text, dm_recap=dm_recap)
        except Exception as exc:
            logger.exception("Turn narration failed for player_id=%s", player_id)
            await self._send_to(
                player_id, self._system_envelope(f"The DM couldn't respond: {exc}", level="error")
            )
            return

        self._session.log.append({"kind": "narration", "text": buffer})
        self._session.append_turn(text, buffer)
        await self._maybe_update_campaign_summary()
        self._session.advance_turn()
        # An absent player's turn slot passes silently - the queue never
        # stalls on whoever isn't at the table.
        await self._skip_absent_players()
        await self._save(player_id)
        await self._broadcast(self._turn_prompt_envelope())

    async def _narrate_and_apply(
        self,
        character: CharacterSheet,
        action_text: str,
        check_for_missed_changes: bool = True,
        dm_recap: str | None = None,
    ) -> str:
        """Runs one DM narrate() call, wiring up apply_update/request_roll/
        update_world and broadcasting narration and state changes as a normal
        turn does. Returns the full narration. Shared by _on_player_action and
        _narrate_opening_scene (a synthetic turn that doesn't consume the queue).

        dm_recap, when given, is prepended to the DM-facing action text,
        invisible to the player."""
        player_id = character.player_id
        # Captured before this turn's apply_update closure can mutate them, so a
        # dying/dead transition can be announced once narration resolves.
        was_dying = character.dying
        was_dead = character.dead
        sheet_changed = False
        npcs_touched: set[str] = set()
        rolls_made: list[dict] = []
        world_changed = False
        # (text, category) per update_character change, broadcast as colour-coded
        # log lines after narration streams (apply_update is sync, can't await).
        outcomes: list[tuple[str, str]] = []
        # (npc_name, xp_awarded, levels_gained) per NPC defeated this turn,
        # broadcast after narration finishes.
        xp_awards: list[tuple[str, int, list[tuple[str, CharacterSheet, int, list[str]]]]] = []
        xp_award_members: dict[str, CharacterSheet] = {}

        def request_roll(update: dict) -> str:
            notation = update.get("dice", "1d20")
            dc = update.get("dc")
            reason = update.get("reason", "")

            # weapon: an equipment name; look up its real SRD damage die rather
            # than trust the model's notation. Unknown name -> given dice unchanged.
            weapon = update.get("weapon")
            damage_type = None
            weapon_magic_bonus = 0
            if weapon:
                equipment_entry = self._rules.get_entry("equipment", weapon)
                weapon_damage = equipment_entry.get("damage") if equipment_entry else None
                if weapon_damage:
                    notation, _, damage_type = weapon_damage.partition(" ")
                # A carried magic instance (InventoryItem.magic_bonus) adds to
                # the damage roll. Damage only - a to-hit roll never names a
                # weapon today. ponytail: to-hit magic bonus, later pass.
                owned_weapon = character.find_item(weapon)
                if owned_weapon:
                    weapon_magic_bonus = owned_weapon.magic_bonus

            # spell: like weapon, but for an attack-roll cantrip/spell. Only
            # spells with "attack": true and a "damage" field resolve anything;
            # save-based/non-damaging spells are a no-op. No known_spells check -
            # this resolves dice for display, it doesn't consume a slot.
            spell = update.get("spell")
            spell_entry = self._rules.get_entry("spell", spell) if spell else None
            if spell_entry and spell_entry.get("attack") and spell_entry.get("damage"):
                notation, _, damage_type = spell_entry["damage"].partition(" ")

            # ability: the character's own ability key; the engine adds its real
            # precomputed modifier rather than trust the DM's arithmetic (and
            # dice.roll's regex only allows one modifier group anyway).
            ability = update.get("ability")

            # skill: the engine resolves its governing ability (SKILL_ABILITIES)
            # automatically. An explicit ability still wins; unknown skill is a
            # no-op.
            skill = update.get("skill")
            if skill not in SKILL_ABILITIES:
                skill = None
            if skill and not ability:
                ability = SKILL_ABILITIES[skill]

            # A resolved attack spell auto-fills ability from the character's
            # spellcasting ability when the DM didn't give one.
            if spell_entry and spell_entry.get("attack") and not ability:
                ability = SPELLCASTING_ABILITY.get(character.character_class.strip().lower())

            ability_mod = character.stat_modifiers.get(ability) if ability else None

            # Proficiency bonus, automatic. Three different 5e rules: a skill
            # check gets it only if proficient in that skill; a spell attack
            # always gets it; a saving throw gets it only for the class's two
            # proficient saves. The DM never tracks proficiencies.
            proficient = bool(skill) and skill in CLASS_SKILL_PROFICIENCIES.get(
                character.character_class.strip().lower(), ()
            )
            proficiency_bonus = character.proficiency_bonus if proficient else 0
            if spell_entry and spell_entry.get("attack"):
                proficient = True
                proficiency_bonus = character.proficiency_bonus
            # Raw input, not the local roll_kind (computed below and never
            # inferred as "save").
            if update.get("roll_kind") == "save" and ability in CLASS_SAVING_THROW_PROFICIENCIES.get(
                character.character_class.strip().lower(), ()
            ):
                proficient = True
                proficiency_bonus = character.proficiency_bonus

            # roll_kind never changes the roll's math; it only narrows which
            # tracked conditions apply disadvantage (ROLL_KIND_DISADVANTAGE_
            # EXCLUSIONS). Inferred from skill/spell-attack when the DM omitted
            # it; unrecognized -> None.
            roll_kind = update.get("roll_kind")
            if roll_kind not in ("attack", "save", "check"):
                if skill:
                    roll_kind = "check"
                elif spell_entry and spell_entry.get("attack"):
                    roll_kind = "attack"
                else:
                    roll_kind = None

            # Automatic from tracked conditions only, never model-supplied.
            # ponytail: untracked circumstantial disadvantage (darkness, cover)
            # not modelled.
            disadvantage_reasons = _has_disadvantage(character, roll_kind)
            disadvantage = bool(disadvantage_reasons)

            # Inspiration: spent here (not by the DM) on the first real d20 roll
            # after the player armed it. dice.roll cancels advantage+disadvantage
            # per 5e, but the token is still consumed - that's the rule.
            inspiration_used = (
                player_id in self._inspiration_armed
                and character.inspiration
                and bool(re.match(r"\s*\d*d20\b", notation, re.IGNORECASE))
            )

            try:
                total, rolls, sides = dice.roll(
                    notation,
                    extra_modifier=(ability_mod or 0) + proficiency_bonus + weapon_magic_bonus,
                    advantage=inspiration_used,
                    disadvantage=disadvantage,
                )
            except dice.InvalidDiceNotation as exc:
                return f"Invalid dice notation: {exc}"

            if inspiration_used:
                character.inspiration = False
                self._inspiration_armed.discard(player_id)
                sheet_changed = True

            success = None if dc is None else total >= dc

            advantage = inspiration_used and not disadvantage
            # Natural 20 on an attack roll. Under advantage/disadvantage `rolls`
            # holds both d20s but only the kept one counts.
            # ponytail: announced only, no automatic damage-doubling.
            kept_roll = (
                max(rolls) if advantage else min(rolls) if disadvantage else rolls[0]
            )
            critical = roll_kind == "attack" and sides == 20 and kept_roll == 20

            roll_entry = {
                "dice": notation, "total": total, "rolls": rolls, "sides": sides,
                "dc": dc, "success": success, "reason": reason,
                "ability": ability, "ability_modifier": ability_mod,
                "damage_type": damage_type, "roll_kind": roll_kind,
                "skill": skill, "proficient": proficient, "proficiency_bonus": proficiency_bonus,
                "spell": spell_entry["name"] if spell_entry and spell_entry.get("attack") else None,
                "disadvantage": disadvantage, "disadvantage_reasons": disadvantage_reasons,
                "advantage": advantage, "inspiration": inspiration_used,
                "critical": critical,
                "weapon_magic_bonus": weapon_magic_bonus or None,
            }
            rolls_made.append(roll_entry)

            label = _dice_roll_tags(roll_entry)
            critical_label = " CRITICAL HIT!" if critical else ""
            if dc is None:
                return f"Rolled {notation}{label}: {total} {rolls}.{critical_label}"
            return (
                f"Rolled {notation}{label}: {total} {rolls} vs DC {dc} — "
                f"{'success' if success else 'failure'}.{critical_label}"
            )

        def apply_update(update: dict) -> str:
            nonlocal sheet_changed

            target = update.get("target") or "self"

            # The model sometimes echoes the sheet's player_id/name/a condition
            # string back as target instead of "self". Catch those here so they
            # route as a self-update rather than spawning a phantom NPC.
            if target in ("self", player_id, character.name) or target in character.conditions:
                result = character.apply_update(update)
                changed = not result.startswith("No changes applied")
                if changed:
                    sheet_changed = True

                cast_spell = update.get("cast_spell")
                if cast_spell:
                    spell_note, spell_changed = _cast_spell(character, cast_spell, self._rules)
                    if spell_changed:
                        sheet_changed = True
                        changed = True
                    if result.startswith("No changes applied"):
                        result = f"{character.name} {spell_note}"
                    else:
                        result += f" {character.name} {spell_note}"

                if changed:
                    category = _outcome_category(update) or ("spell" if cast_spell else None)
                    if category:
                        outcomes.append((f"{character.name}: {result}", category))

                return result

            # Casefold and fold underscores so "Bandit"/"bandit"/"second_bandit"
            # don't spawn duplicate NPC entries across turns. npc.name keeps
            # first-seen casing for display.
            npc_key = target.casefold().replace("_", " ")
            npc = self._session.npcs.get(npc_key)
            introduced = npc is None

            if introduced:
                # Safety net for when the DM forgets a real max_hp from lookup_rule.
                max_hp = update.get("max_hp") or DEFAULT_NPC_HP
                npc = CharacterSheet(player_id=target, name=target, hp=max_hp, max_hp=max_hp)
                # Give a known SRD monster its real stat block (and therefore
                # real request_roll modifiers) for free.
                monster_entry = self._rules.get_entry("monster", target)
                if monster_entry is not None:
                    npc.stats = dict(monster_entry.get("stats", {}))
                    # A monster's AC is a flat authored value, copied directly
                    # (unlike a player's computed AC). Default 10 if absent.
                    if "ac" in monster_entry:
                        npc.ac = monster_entry["ac"]
                self._session.npcs[npc_key] = npc

            # Captured before apply_update mutates hp: hp crossing >0 -> 0 is the
            # deterministic XP trigger, not a tool the DM must remember to call.
            was_alive = npc.hp > 0

            delta_result = npc.apply_update(update)
            changed = not delta_result.startswith("No changes applied")
            defeated = was_alive and npc.hp == 0

            # Introducing an NPC is a real change worth broadcasting even if
            # this call's deltas were a no-op.
            if introduced or changed:
                npcs_touched.add(npc_key)

            if changed:
                category = _outcome_category(update)
                if category:
                    outcomes.append((f"{npc.name}: {delta_result}", category))

            xp_note = ""
            if defeated:
                # _award_party_xp floor-divides across the party; the remainder
                # drops, a solo session awards everything to the one member.
                xp_award = _xp_for_npc(npc, update, self._rules)
                party_results = self._award_party_xp(xp_award)
                for _pid, _member, _levels, _asi in party_results:
                    xp_award_members[_pid] = _member
                sheet_changed = True
                xp_awards.append((npc.name, xp_award, party_results))
                xp_note = f" {npc.name} is defeated! The party gains {xp_award} XP."
                for _pid, _member, levels_gained, asi_abilities in party_results:
                    if levels_gained:
                        xp_note += f" {_member.name} reaches level {_member.level}!"
                    xp_note += _asi_announcement(_member.name, asi_abilities)

            if introduced:
                intro = f"Introduced {npc.name} (HP {npc.hp}/{npc.max_hp})."
                result = f"{intro} {delta_result}" if changed else intro
            else:
                result = delta_result
            return result + xp_note

        # Scene-facts snapshot (docs/protocol.md "Scene envelope"): the decide
        # phase reports through scene_sink; broadcast after narration finishes.
        scene_facts: dict = {}

        def scene_sink(facts: dict) -> None:
            nonlocal scene_facts
            # The 4-action cap and the {text, skill?, dc?} shape are both
            # protocol guarantees (docs/protocol.md), enforced here
            # server-side rather than trusted to each backend.
            trimmed = dict(facts)
            trimmed["suggested_actions"] = _sanitize_suggested_actions(facts.get("suggested_actions", []))
            scene_facts = trimmed

        def fact_sink(facts: list) -> None:
            self._session.add_facts(facts)

        full_clocks_before = {c.name for c in self._session.world.clocks if c.filled >= c.segments}
        # Progress clocks filled to their last segment this turn (docs/protocol.md
        # "Clocks"), announced after narration finishes.
        clocks_filled: list[str] = []

        def update_world(update: dict) -> str:
            nonlocal world_changed
            result = self._session.world.apply_update(update)
            if not result.startswith("No changes applied"):
                world_changed = True
                for clock in self._session.world.clocks:
                    if clock.name not in full_clocks_before and clock.filled >= clock.segments:
                        clocks_filled.append(f"The clock '{clock.name}' fills - its consequence arrives.")
                        full_clocks_before.add(clock.name)
            return result

        # Prepended to this call's action_text (invisible to players) rather
        # than the shared system prompt, since content_preference is per-session.
        # "standard" has no hint -> no-op for the default case.
        hint = CONTENT_PREFERENCE_HINTS.get(self._session.content_preference)
        narrate_action_text = f"[{hint}]\n{action_text}" if hint else action_text
        # dm_recap goes in front of the tone hint - "what's going on" before
        # "how to say it".
        if dm_recap:
            narrate_action_text = (
                f"[Context: {character.name} is picking back up after a gap - {dm_recap}]\n{narrate_action_text}"
            )

        buffer = ""
        world_summary = self._session.world.narrator_context()
        if self._session.campaign_summary:
            recap = f"Campaign so far: {self._session.campaign_summary}"
            world_summary = f"{recap}\n{world_summary}" if world_summary else recap
        npc_roster = _npc_roster(self._session)
        if npc_roster:
            world_summary = f"{world_summary}\n{npc_roster}" if world_summary else npc_roster
        # Lorebook injection (docs/protocol.md "World context -> lorebook"):
        # keyword hits from the recent play window under a character budget.
        # Empty selection -> empty block.
        window_text = "\n".join(
            str(message.get("content", "")) for message in self._session.history[-6:]
        )
        lore_block = self._lorebook.injection_block(f"{window_text}\n{npc_roster}\n{world_summary}")
        if not lore_block:
            lore_block = self._lorebook.injection_block(narrate_action_text)
        if lore_block:
            world_summary = f"{world_summary}\n\n{lore_block}" if world_summary else lore_block
        ledger_block = self._ledger_context(narrate_action_text)
        if ledger_block:
            world_summary = f"{world_summary}\n\n{ledger_block}" if world_summary else ledger_block
        sink_kwargs: dict = {}
        if getattr(self._dm, "supports_scene_facts", False):
            sink_kwargs["scene_sink"] = scene_sink
        if getattr(self._dm, "supports_fact_ledger", False):
            sink_kwargs["fact_sink"] = fact_sink
        async for chunk in self._dm.narrate(
            history=self._session.history,
            character_summary=character.model_dump_json(),
            action_text=narrate_action_text,
            apply_update=apply_update,
            request_roll=request_roll,
            update_world=update_world,
            world_summary=world_summary,
            **sink_kwargs,
        ):
            buffer += chunk
            await self._broadcast(self._log_envelope("narration", chunk, done=False))
        await self._broadcast(self._log_envelope("narration", "", done=True))

        if scene_facts:
            await self._broadcast(
                Envelope(
                    type="scene_update",
                    session_id=self._session.session_id,
                    sender_id="server",
                    payload={"narration_id": f"{player_id}:{len(self._session.log)}", **scene_facts},
                )
            )

        # A real chance for the DM to self-correct (see check_for_missed_changes
        # below). Must run before the sheet/outcome broadcasts so a correction's
        # apply_update is picked up by them. getattr - optional capability, most
        # test doubles skip it. No regex gate here (unlike the player-facing
        # warning below): a clean check just costs one structured-output call.
        missed_change_corrected = False
        if check_for_missed_changes and not sheet_changed and not npcs_touched:
            check_missed_change = getattr(self._dm, "check_missed_change", None)
            if check_missed_change is not None:
                missed_change_corrected = await check_missed_change(buffer, character.model_dump_json(), apply_update)

        for roll in rolls_made:
            await self._broadcast(self._log_envelope("dice", self._dice_log_text(character.name, roll)))
            await self._broadcast(self._dice_result_envelope(player_id, roll))

        # Colour-coded so damage/heal/spell/item reads differently at a glance.
        # Broadcast (not owner-only) - HP/conditions changing is already public.
        for text, category in outcomes:
            await self._broadcast(self._log_envelope("outcome", text, category=category))

        # A filled clock is a stakes milestone, not narration - announced as
        # a system_message (docs/protocol.md "Clocks") so clients can render
        # it distinctly and react.
        for clock_text in clocks_filled:
            await self._broadcast(self._system_envelope(clock_text, level="info"))

        if xp_award_members:
            for _pid, _member in xp_award_members.items():
                await self._send_to(_pid, self._character_update_envelope(_pid, _member))
                await self._broadcast(self._player_update_envelope(_member))
        elif sheet_changed:
            await self._send_to(player_id, self._character_update_envelope(player_id, character))
            # character_update is the owner's full sheet; player_update keeps
            # everyone else's public-only presence view live.
            await self._broadcast(self._player_update_envelope(character))

        for npc_key in npcs_touched:
            touched_npc = self._session.npcs[npc_key]
            await self._broadcast(self._npc_update_envelope(touched_npc.name, touched_npc))

        # After narration/sheet/npc updates, so it reads as the resolution.
        for npc_name, xp_award, party_results in xp_awards:
            text = _party_xp_announcement(npc_name, xp_award, party_results)
            await self._broadcast(self._system_envelope(text, level="info"))

        # Announce a dying/dead transition explicitly. Compared against
        # was_dying/was_dead from before narration so an already-dying
        # character isn't re-announced every turn.
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

        if missed_change_corrected:
            # A real correction landed via check_missed_change - tell the player
            # the sheet was fixed. Not advisory=True (that's the yellow-triangle
            # "double check this" treatment, wrong for a done fix).
            await self._send_to(
                player_id,
                self._system_envelope(
                    "The DM double-checked that last narration and updated the sheet to match.",
                    level="info",
                ),
            )
        elif (
            check_for_missed_changes
            and not sheet_changed
            and not npcs_touched
            and POSSIBLE_UNTRACKED_CHANGE_PATTERN.search(buffer)
        ):
            proposed = None
            propose_correction = getattr(self._dm, "propose_correction", None)
            if propose_correction is not None:
                proposed = await propose_correction(buffer, character.model_dump_json())
            if proposed:
                self._pending_proposals[player_id] = proposed
            await self._send_to(
                player_id,
                self._system_envelope(
                    "The DM's narration may describe a change that wasn't recorded - "
                    "your sheet might be out of sync with the story.",
                    level="warning",
                    advisory=True,
                    proposed_change=proposed,
                ),
            )

        return buffer

    def _ledger_context(self, action_text: str) -> str:
        """Render the fact ledger as a world_summary block (docs/REBUILD_PLAN.md
        watchlist: AriGraph-lite). Newest LEDGER_RECENT_LIMIT facts always go;
        older ones only when a 5+-char word of theirs appears in the current
        action text or location - an old promise resurfaces when it's named,
        not on every turn. Empty string when there's nothing to say."""
        facts = self._session.fact_ledger
        if not facts:
            return ""
        recent = facts[-LEDGER_RECENT_LIMIT:]
        older = facts[:-LEDGER_RECENT_LIMIT]
        probe = f"{action_text}\n{self._session.world.location or ''}".casefold()
        relevant: list[str] = []
        for fact in reversed(older):
            words = {
                w.strip(".,;:!?'\"")
                for w in fact.casefold().split()
                if len(w.strip(".,;:!?'\"")) >= 5
            }
            if any(w in probe for w in words):
                relevant.insert(0, fact)
                if len(relevant) >= LEDGER_RELEVANT_LIMIT:
                    break
        lines = relevant + [f for f in recent]
        if not lines:
            return ""
        return "Session facts worth remembering:\n" + "\n".join(f"- {line}" for line in lines)

    async def _maybe_update_campaign_summary(self) -> None:
        """Best-effort rolling campaign summary (docs/REBUILD_PLAN.md): every
        CAMPAIGN_SUMMARY_INTERVAL resolved turns, let the backend compress the
        summary plus the history window. Failure never blocks the turn."""
        session = self._session
        if not session.history:
            return
        session.turns_since_summary += 1
        if session.turns_since_summary < CAMPAIGN_SUMMARY_INTERVAL:
            return
        session.turns_since_summary = 0
        await self._refresh_campaign_summary()

    async def _refresh_campaign_summary(self) -> None:
        """Compress history into campaign_summary now, ignoring the interval.
        Used on the scheduled tick (via _maybe_update_campaign_summary) and
        when an adventure ends. Best-effort; never raises."""
        session = self._session
        summarize = getattr(self._dm, "summarize", None)
        if summarize is None or not session.history:
            return
        try:
            summary = await summarize(session.campaign_summary, session.history)
            if summary:
                session.campaign_summary = summary
        except Exception:
            logger.exception("Campaign summary update failed for session %s", session.session_id)

    async def _on_chat_message(self, envelope: Envelope) -> None:
        text = envelope.payload.get("text", "")
        await self._broadcast(self._log_envelope("chat", text))
        # Sending a message means you're present and not mid-typing.
        await self._broadcast(self._presence_envelope(envelope.sender_id, typing=False))
        # A rate-limited tavern-keeper reply, lobby only. Skips near-empty lines.
        if not self._has_started() and len(text.strip()) >= 4:
            await self._maybe_keeper_line("chat", recent=text)

    async def _maybe_keeper_line(self, trigger: str, recent: str = "") -> None:
        """Best-effort lobby tavern-keeper chatter. Rate-limited hard (one line
        per KEEPER_MIN_INTERVAL, greeting bypasses only on the very first call).
        No-op without a backend `tavern_line`, or once the adventure has started.
        Any failure is swallowed - the keeper is decoration, never load-bearing."""
        if self._has_started():
            return
        tavern_line = getattr(self._dm, "tavern_line", None)
        if tavern_line is None:
            return
        now = time.monotonic()
        if now - self._last_keeper_ts < KEEPER_MIN_INTERVAL:
            return
        self._last_keeper_ts = now
        keeper = self._world_bible.tavern_keeper
        party = ", ".join(
            " ".join(p for p in (c.name, "the", c.race, c.character_class) if p)
            for c in self._session.characters.values()
        )
        try:
            line = (await tavern_line({
                "trigger": trigger,
                "keeper": {"name": keeper.name, "persona": keeper.persona},
                "party": party,
                "recent_chat": recent,
                "campaign_summary": self._session.campaign_summary or self._session.world.summary,
            })).strip()
        except Exception:
            logger.exception("Tavern-keeper line failed")
            return
        if line:
            await self._broadcast(self._log_envelope("keeper", f"{keeper.name}: {line}"))

    async def _on_player_ready(self, envelope: Envelope) -> None:
        """Lobby ready-check toggle. When every connected player is ready the
        adventure auto-starts; anyone can still force it early via start_session.
        A no-op once the adventure has started."""
        if self._has_started():
            return
        player_id = envelope.sender_id
        if player_id not in self._session.characters:
            return
        ready = bool(envelope.payload.get("ready"))
        in_list = player_id in self._session.ready_players
        if ready and not in_list:
            self._session.ready_players.append(player_id)
        elif not ready and in_list:
            self._session.ready_players.remove(player_id)
        else:
            return
        await self._broadcast(self._player_update_envelope(self._session.characters[player_id]))
        await self._save(player_id)

        connected = self._connected_players & self._session.characters.keys()
        if connected and connected <= set(self._session.ready_players):
            await self._on_start_session(envelope)

    async def _on_tavern_rest(self, envelope: Envelope) -> None:
        """A long rest taken in the lobby, between adventures - full HP, half
        the hit-dice pool back, spell slots refilled, for every party character.
        A no-op once the adventure has started (use the DM's `rest` field then).
        Idempotent: a party already rested just gets "nothing to recover"."""
        if self._has_started() or not self._session.characters:
            return
        rested = []
        for player_id, character in self._session.characters.items():
            result = character.apply_update({"rest": "long"})
            if not result.startswith("No changes applied"):
                rested.append(character.name)
                await self._send_to(player_id, self._character_update_envelope(player_id, character))
                await self._broadcast(self._player_update_envelope(character))
        if rested:
            await self._broadcast(self._system_envelope(
                "The party takes a long rest. Wounds close, spells return.", level="info"
            ))
            await self._save(envelope.sender_id)
        else:
            await self._send_to(envelope.sender_id, self._system_envelope(
                "Everyone's already rested - nothing to recover.", level="info"
            ))

    async def _on_set_typing(self, envelope: Envelope) -> None:
        """Ephemeral lobby typing indicator - broadcast, never stored. The
        client re-sends while still typing and auto-hides on its own if a
        'false' is dropped, so no server-side expiry sweep is needed."""
        await self._broadcast(
            self._presence_envelope(envelope.sender_id, typing=bool(envelope.payload.get("typing")))
        )

    async def _on_character_edit(self, envelope: Envelope) -> None:
        """Player-side bookkeeping that doesn't need DM adjudication: the RP
        text fields, removing/equipping/using inventory by name (docs/
        protocol.md). Never touches hp/conditions/stats/xp except use_item's
        own narrow, deliberate exception (see CHARACTER_EDIT_FIELDS above) -
        a player can't grant themselves gear or healing out of nowhere, only
        equip or consume what's already in their inventory. Exempt from turn
        order."""
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
                self._system_envelope(
                    "Can't edit '{}' - try {}, remove_item, equip, unequip, or use_item.".format(
                        field, ", ".join(sorted(CHARACTER_EDIT_TEXT_FIELDS))
                    ),
                    level="warning",
                ),
            )
            return

        ac_changed = False
        hp_changed = False

        if field in CHARACTER_EDIT_TEXT_FIELDS:
            setattr(character, field, str(value))
        elif field == "remove_item":
            item = str(value)
            if not character.remove_item(item):
                await self._send_to(
                    player_id, self._system_envelope(f"You don't have '{item}' to remove.", level="warning")
                )
                return
            # Removing the last of an item unequips it, so no slot dangles.
            if character.find_item(item) is None:
                if character.equipped_weapon == item:
                    character.equipped_weapon = None
                if character.equipped_armor == item:
                    character.equipped_armor = None
                    ac_changed = True
                if character.equipped_shield == item:
                    character.equipped_shield = None
                    ac_changed = True
        elif field == "equip":
            item = str(value)
            if character.find_item(item) is None:
                await self._send_to(
                    player_id, self._system_envelope(f"You don't have '{item}' to equip.", level="warning")
                )
                return
            slot = _equip_slot_for(item, self._rules)
            if slot == "weapon":
                character.equipped_weapon = item
            elif slot == "armor":
                character.equipped_armor = item
                ac_changed = True
            elif slot == "shield":
                character.equipped_shield = item
                ac_changed = True
            else:
                await self._send_to(
                    player_id,
                    self._system_envelope(f"'{item}' isn't a recognized weapon, armor, or shield.", level="warning"),
                )
                return
        elif field == "use_item":
            item = str(value)
            message, changed = _use_item(character, item)
            if not changed:
                await self._send_to(player_id, self._system_envelope(message.capitalize(), level="warning"))
                return
            hp_changed = True
            await self._send_to(
                player_id, self._system_envelope(f"{character.name} {message}", level="info")
            )
        elif field == "cast_spell":
            # Bookkeeping only, same limit _cast_spell's own docstring names -
            # spends a real slot, never resolves the spell's in-fiction
            # effect. The player still narrates the action in the normal
            # action box; this just tracks the resource. Spell slots are
            # owner-only (never in _public_character_view), so no public
            # broadcast is needed the way use_item's HP change needed one.
            spell = str(value)
            message, changed = _cast_spell(character, spell, self._rules)
            # changed=False covers both a real failure (unknown spell, not
            # known, no slots left) and a cantrip (nothing to spend, not an
            # error) - _cast_spell's own message text is the only thing that
            # tells the two apart. The client always shows one Cast button
            # per known spell regardless of level (it has no per-spell level
            # data to decide otherwise), so clicking one for a cantrip is a
            # real, common, non-error case here.
            if not changed and not message.endswith("(cantrip)."):
                await self._send_to(player_id, self._system_envelope(message.capitalize(), level="warning"))
                return
            await self._send_to(
                player_id, self._system_envelope(f"{character.name} {message}", level="info")
            )
        elif field == "unequip":
            item = str(value)
            if character.equipped_weapon == item:
                character.equipped_weapon = None
            elif character.equipped_armor == item:
                character.equipped_armor = None
                ac_changed = True
            elif character.equipped_shield == item:
                character.equipped_shield = None
                ac_changed = True
            else:
                await self._send_to(
                    player_id, self._system_envelope(f"You don't have '{item}' equipped.", level="warning")
                )
                return

        if ac_changed:
            armor_item = character.find_item(character.equipped_armor)
            shield_item = character.find_item(character.equipped_shield)
            character.ac = _compute_ac(
                character.equipped_armor, character.stat_modifiers.get("dex", 0), self._rules,
                character.equipped_shield,
                armor_magic_bonus=armor_item.magic_bonus if armor_item else 0,
                shield_magic_bonus=shield_item.magic_bonus if shield_item else 0,
            )

        # The edited fields are private, but ac/hp are public - so an equip
        # that changed AC, or a use_item that changed HP, needs the public
        # player_update broadcast too.
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        if ac_changed or hp_changed:
            await self._broadcast(self._player_update_envelope(character))
        await self._save(player_id)

    async def _on_generate_portrait(self, envelope: Envelope) -> None:
        """Player-initiated portrait generation - an explicit click, not
        automatic at character creation, matching this project's "always
        asks first" stance and the real GPU cost/latency involved. Exempt
        from turn order, same as character_edit. Repeatable - clicking
        again just regenerates, no one-time gate."""
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        if character is None:
            await self._send_to(
                player_id,
                self._system_envelope("You don't have a character to generate a portrait for yet.", level="warning"),
            )
            return
        if self._image_backend is None:
            await self._send_to(
                player_id,
                self._system_envelope("Portrait generation isn't configured on this server.", level="warning"),
            )
            return

        style = envelope.payload.get("style", DEFAULT_STYLE)
        prompt = build_portrait_prompt(character, style=style)

        async def on_progress(step: int, total: int) -> None:
            await self._send_to(player_id, self._portrait_progress_envelope(step, total))

        try:
            image_bytes = await self._image_backend.generate_portrait(prompt, on_progress=on_progress)
        except Exception:
            logger.exception("Portrait generation failed for player_id=%s", player_id)
            await self._send_to(
                player_id, self._system_envelope("Couldn't generate a portrait right now.", level="warning")
            )
            return

        character.portrait = f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        # Public - a character's appearance is the same kind of
        # party-visible fact as name/class/HP (see _public_character_view).
        await self._broadcast(self._player_update_envelope(character))
        await self._save(player_id)

    async def _on_apply_proposed_change(self, envelope: Envelope) -> None:
        """Applies a correction proposal the player accepted via /apply
        (docs/protocol.md's "Missed-change confirmable proposal"). Generated by
        the DM backend when check_missed_change declined to auto-correct; carries
        the MISSED_CHANGE_SCHEMA fields. Exempt from turn order. Applies through
        the same apply_update/NPC machinery as a real tool call. The proposal
        expires on the player's next action."""
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        if character is None:
            await self._send_to(
                player_id, self._system_envelope("You don't have a character to correct yet.", level="warning")
            )
            return
        proposal = self._pending_proposals.pop(player_id, None)
        if not proposal:
            await self._send_to(
                player_id,
                self._system_envelope(
                    "Nothing to apply - the correction suggestion is no longer pending.", level="info"
                ),
            )
            return

        target = proposal.get("target") or "self"
        changed = False
        if target in ("self", player_id, character.name):
            result = character.apply_update(proposal)
            changed = not result.startswith("No changes applied")
            if changed:
                await self._send_to(player_id, self._character_update_envelope(player_id, character))
                await self._broadcast(self._player_update_envelope(character))
                await self._broadcast(self._log_envelope("outcome", f"{character.name}: {result}"))
        else:
            # Same key normalization as the in-turn apply_update path (see
            # there) - the two call sites must not drift.
            npc_key = target.casefold().replace("_", " ")
            npc = self._session.npcs.get(npc_key)
            introduced = npc is None
            if introduced:
                max_hp = proposal.get("max_hp") or DEFAULT_NPC_HP
                npc = CharacterSheet(player_id=target, name=target, hp=max_hp, max_hp=max_hp)
                monster_entry = self._rules.get_entry("monster", target)
                if monster_entry is not None:
                    npc.stats = dict(monster_entry.get("stats", {}))
                    if "ac" in monster_entry:
                        npc.ac = monster_entry["ac"]
                self._session.npcs[npc_key] = npc
            was_alive = npc.hp > 0
            delta_result = npc.apply_update(proposal)
            changed = not delta_result.startswith("No changes applied")
            defeated = was_alive and npc.hp == 0
            if introduced or changed:
                await self._broadcast(self._npc_update_envelope(npc.name, npc))
            if changed:
                await self._broadcast(self._log_envelope("outcome", f"{npc.name}: {delta_result}"))
            if defeated:
                xp_award = _xp_for_npc(npc, proposal, self._rules)
                party_results = self._award_party_xp(xp_award)
                text = _party_xp_announcement(npc.name, xp_award, party_results)
                await self._broadcast(self._system_envelope(text, level="info"))
                for _pid, _member, _levels, _asi in party_results:
                    await self._send_to(_pid, self._character_update_envelope(_pid, _member))
                    await self._broadcast(self._player_update_envelope(_member))

        await self._send_to(
            player_id,
            self._system_envelope(
                "Correction applied - the sheet now matches the narration."
                if changed else "Nothing changed - the suggestion didn't alter the sheet.",
                level="info",
            ),
        )

    async def _on_death_save(self, envelope: Envelope) -> None:
        """A dying player's own roll against death (docs/protocol.md's "Death
        saves"). Its own event, not folded into dice_roll: always a fixed 1d20,
        and carries outcome bookkeeping (successes/failures/stabilize/died) no
        other roll has.

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

        # Natural 20: regain 1 HP and wake immediately, ending the dying state
        # regardless of accumulated successes/failures (5e's rule).
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

        # dc=10 reuses dice_result's existing dc/success rendering as-is.
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

    async def _on_use_inspiration(self, envelope: Envelope) -> None:
        """Toggle whether the player's held Inspiration is armed for their next
        d20 roll. Exempt from turn order like death_save - the engine spends the
        token in request_roll when that roll actually happens."""
        player_id = envelope.sender_id
        character = self._session.characters.get(player_id)
        if character is None:
            return
        if not character.inspiration:
            self._inspiration_armed.discard(player_id)
            await self._send_to(
                player_id, self._system_envelope("You have no Inspiration to use.", level="warning")
            )
            return
        if player_id in self._inspiration_armed:
            self._inspiration_armed.discard(player_id)
            await self._send_to(
                player_id, self._system_envelope("Inspiration set aside - it won't be used.", level="info")
            )
        else:
            self._inspiration_armed.add(player_id)
            await self._send_to(
                player_id,
                self._system_envelope(
                    "Inspiration ready - your next d20 roll is made with advantage.", level="info"
                ),
            )

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
        # Shared label part via _dice_roll_tags; reason and the crit callout
        # stay specific to this function.
        label = _dice_roll_tags(roll)
        reason_label = f" ({roll['reason']})" if roll["reason"] else ""
        critical_label = " CRITICAL HIT!" if roll.get("critical") else ""
        text = f"{name} rolls {roll['dice']}{label}{reason_label}: {roll['total']} {roll['rolls']}"
        if roll["dc"] is not None:
            text += f" vs DC {roll['dc']}"
        if roll["success"] is not None:
            text += " — success" if roll["success"] else " — failure"
        text += critical_label
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
        if roll.get("roll_kind"):
            payload["roll_kind"] = roll["roll_kind"]
        if roll.get("skill"):
            payload["skill"] = roll["skill"]
            payload["proficient"] = roll["proficient"]
            if roll["proficient"]:
                payload["proficiency_bonus"] = roll["proficiency_bonus"]
        if roll.get("spell"):
            payload["spell"] = roll["spell"]
            payload["proficiency_bonus"] = roll["proficiency_bonus"]
        # A save carries proficient/proficiency_bonus with no skill/spell field
        # to hang them on, so surface them here directly.
        if roll.get("roll_kind") == "save" and not roll.get("skill") and not roll.get("spell"):
            payload["proficient"] = roll["proficient"]
            if roll["proficient"]:
                payload["proficiency_bonus"] = roll["proficiency_bonus"]
        if roll.get("disadvantage"):
            payload["disadvantage"] = True
            payload["disadvantage_reasons"] = roll["disadvantage_reasons"]
        if roll.get("advantage"):
            payload["advantage"] = True
        if roll.get("inspiration"):
            payload["inspiration"] = True
        if roll.get("critical"):
            payload["critical"] = True
        if roll.get("weapon_magic_bonus"):
            payload["weapon_magic_bonus"] = roll["weapon_magic_bonus"]
        return Envelope(
            type="dice_result", session_id=self._session.session_id, sender_id="server", payload=payload
        )

    def _state_sync_envelope(self, recipient_id: str) -> Envelope:
        return Envelope(
            type="state_sync",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                # Recipient's own entry is the full sheet; everyone else's is
                # the public view.
                "characters": {
                    pid: (
                        _owner_character_view(c, self._rules)
                        if pid == recipient_id
                        else _public_character_view(c)
                    )
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
                "in_combat": self._session.in_combat,
                "log_tail": self._session.log[-20:],
                # _has_started(), not the raw field - covers the empty-log
                # started case (see _has_started()).
                "started": self._has_started(),
                "ready_players": list(self._session.ready_players),
                "adventures_completed": self._session.adventures_completed,
                "quest_hooks": list(self._session.quest_hooks),
                "hook_votes": {h: list(v) for h, v in self._session.hook_votes.items()},
            },
        )

    def _character_update_envelope(self, player_id: str, character: CharacterSheet) -> Envelope:
        return Envelope(
            type="character_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "sheet_delta": _owner_character_view(character, self._rules)},
        )

    def _portrait_progress_envelope(self, step: int, total: int) -> Envelope:
        return Envelope(
            type="portrait_progress",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"step": step, "total": total},
        )

    def _player_joined_envelope(self, character: CharacterSheet) -> Envelope:
        # Broadcast, public-view-only payload.
        return Envelope(
            type="player_joined",
            session_id=self._session.session_id,
            sender_id="server",
            payload=self._public_view_with_lobby(character),
        )

    def _public_view_with_lobby(self, character: CharacterSheet) -> dict:
        # The shared public view plus the lobby ready flag (session state, so
        # it can't live on _public_character_view, which only sees the sheet).
        return {**_public_character_view(character), "ready": character.player_id in self._session.ready_players}

    def _player_left_envelope(self, player_id: str, name: str) -> Envelope:
        return Envelope(
            type="player_left",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "name": name},
        )

    def _presence_envelope(self, player_id: str, typing: bool) -> Envelope:
        return Envelope(
            type="presence",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"player_id": player_id, "typing": typing},
        )

    def _session_started_envelope(self) -> Envelope:
        # Empty payload - a lifecycle signal to leave the lobby view. Fires
        # unconditionally, not inferred from the first narration (best-effort).
        return Envelope(
            type="session_started",
            session_id=self._session.session_id,
            sender_id="server",
            payload={},
        )

    def _session_ended_envelope(self) -> Envelope:
        # The inverse lifecycle signal: the adventure wrapped, drop back to the
        # tavern lobby. Empty payload; the client keeps its state and re-renders
        # LobbyScreen off `started`. adventures_completed rides the next state_sync.
        return Envelope(
            type="session_ended",
            session_id=self._session.session_id,
            sender_id="server",
            payload={},
        )

    def _quest_board_envelope(self) -> Envelope:
        return Envelope(
            type="quest_board",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                "hooks": list(self._session.quest_hooks),
                "votes": {h: list(v) for h, v in self._session.hook_votes.items()},
            },
        )

    def _player_update_envelope(self, character: CharacterSheet) -> Envelope:
        # Public counterpart to the private full-sheet _character_update_envelope.
        return Envelope(
            type="player_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload=self._public_view_with_lobby(character),
        )

    def _npc_update_envelope(self, name: str, npc: CharacterSheet) -> Envelope:
        # Broadcast - an NPC's wounds/conditions are shared observable fiction.
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
        # in_combat/turn_order ride along so an in-combat indicator stays live
        # without a fresh state_sync.
        return Envelope(
            type="turn_prompt",
            session_id=self._session.session_id,
            sender_id="server",
            payload={
                "player_id": self._session.current_turn,
                "prompt_text": "What do you do?",
                "in_combat": self._session.in_combat,
                "turn_order": self._session.turn_order,
            },
        )

    def _log_envelope(
        self, kind: str, text: str, done: bool | None = None, category: str | None = None
    ) -> Envelope:
        payload: dict = {"kind": kind, "text": text}
        if done is not None:
            payload["done"] = done
        # category (damage/heal/spell/condition/item) is its own field: kind
        # says how to route the line, category says what colour.
        if category is not None:
            payload["category"] = category
        return Envelope(type="log_entry", session_id=self._session.session_id, sender_id="server", payload=payload)

    def _system_envelope(self, text: str, level: str = "info", advisory: bool = False, proposed_change: dict | None = None) -> Envelope:
        # advisory: only the missed-change heuristic sets it - a "double check
        # this" nudge, distinct from a plain warning. Included only when true.
        payload: dict = {"level": level, "text": text}
        if advisory:
            payload["advisory"] = True
        if proposed_change is not None:
            payload["proposed_change"] = proposed_change
        return Envelope(
            type="system_message",
            session_id=self._session.session_id,
            sender_id="server",
            payload=payload,
        )
