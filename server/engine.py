from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import ValidationError

from shared.protocol import Envelope

from . import dice
from .lore import (
    OriginTable,
    WorldBible,
    load_default_origin_table,
    load_default_world_bible,
    random_origin,
)
from .lorebook import MAX_LORE_CHARS, SUPPORTED_SUFFIXES, Lorebook
from .narrator import NarratorBackend
from .persistence import SessionStore
from .rules import RulesIndex, slug
from .state import (
    ABILITY_KEYS,
    SKILL_ABILITIES,
    SPELLCASTING_ABILITY,
    CharacterSheet,
    InventoryItem,
    Session,
    ability_modifier,
)

logger = logging.getLogger(__name__)

Broadcast = Callable[[Envelope], Awaitable[None]]
SendTo = Callable[[str, Envelope], Awaitable[None]]

# Mirrors _on_join_session's own hp=10, max_hp=10 fallback for a fresh
# player character - the safety net for an NPC introduced without a
# real max_hp from lookup_rule, not the intended path.
DEFAULT_NPC_HP = 10

# How many resolved turns between campaign-summary rebuilds - the rolling
# window (Session.max_history_messages) holds ~6 turns, so 10 keeps the
# summary comfortably ahead of what would otherwise scroll out of context.
CAMPAIGN_SUMMARY_INTERVAL = 10

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
# away.
DISADVANTAGE_CONDITIONS = frozenset({"poisoned", "frightened", "prone"})

# Per-condition roll_kind exclusions, matching each condition's real SRD
# text now that request_roll can actually distinguish a save from a check
# from an attack roll (the roll_kind field, AnthropicNarrator only - see
# ROADMAP.md). Poisoned/frightened's real text is "disadvantage on attack
# rolls and ability checks" - saving throws are never mentioned, so a
# save is excluded. Prone's real text only ever mentions attack rolls
# ("has disadvantage on attack rolls") - neither checks nor saves are
# affected at all, so both are excluded. Only takes effect when roll_kind
# is actually given (see _has_disadvantage below) - an omitted roll_kind
# (a plain player /roll, or any older/simpler request_roll call that
# doesn't set it) keeps the prior broader "applies to any roll" behavior
# unchanged, so this is additive, not a breaking change to existing calls.
ROLL_KIND_DISADVANTAGE_EXCLUSIONS: dict[str, frozenset[str]] = {
    "poisoned": frozenset({"save"}),
    "frightened": frozenset({"save"}),
    "prone": frozenset({"save", "check"}),
}

# character_edit's own real scope (docs/protocol.md, ROADMAP.md's "let a
# player edit their own notes/inventory directly, without DM adjudication"):
# deliberately just the fields that are pure player-side bookkeeping, not
# mechanical state. hp/conditions/stats/xp all stay DM- or engine-only -
# the same "the engine or the DM decides mechanical state, the player only
# decides fiction/bookkeeping" boundary update_character's own tool schema
# already draws, just enforced from the other direction here. equip/
# unequip are the one field pair that also changes a mechanical value
# (ac) as a side effect - still consistent with that boundary, since the
# player only ever names which owned item to wear/wield, never the AC
# number itself; _compute_ac (engine-owned, real SRD data) computes what
# that actually means, the player never types a value into it directly.
CHARACTER_EDIT_FIELDS = frozenset({"notes", "add_item", "remove_item", "equip", "unequip"})


def _has_disadvantage(character: CharacterSheet, roll_kind: str | None = None) -> list[str]:
    """Returns which of the acting character's current conditions actually
    trigger disadvantage (empty if none) - a list, not just a bool, so a
    caller can name the real reason in the roll's own text rather than a
    bare "disadvantage" with no explanation. Real 5e disadvantage never
    stacks (multiple sources still just apply once) - callers only need
    `bool(...)` on this to know whether to roll with disadvantage at all,
    the list itself is purely for the human-readable reason.

    roll_kind (optional - "attack"/"save"/"check", request_roll's own new
    field) narrows this against ROLL_KIND_DISADVANTAGE_EXCLUSIONS when
    given; left as the prior "applies regardless" behavior when omitted,
    which is what a plain player /roll (no roll_kind at all) always
    passes."""
    reasons = []
    for c in character.conditions:
        key = c.casefold()
        if key not in DISADVANTAGE_CONDITIONS:
            continue
        if roll_kind is not None and roll_kind in ROLL_KIND_DISADVANTAGE_EXCLUSIONS.get(key, frozenset()):
            continue
        reasons.append(c)
    return reasons


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

# Deterministic per-class known spells, the same "no player-chosen
# allocation yet" approach CLASS_STARTING_EQUIPMENT/CLASS_SKILL_PROFICIENCIES
# already take - real 5e lets a wizard/cleric prepare a chosen subset daily
# from a much larger list; this assigns a small, fixed set once at creation
# instead of modeling that choice or the daily re-preparation ritual. A
# leveled spell above what the character can currently cast (server/rules/
# srd.json's spell_slots_by_level - e.g. fireball at level 1, no 3rd-level
# slot until level 5) is still "known", simply not castable yet until a
# real slot exists - no special-casing needed, the slot check at cast time
# is the only gate. Fighter/rogue have no entry (cast nothing), the same
# fallback CLASS_ABILITY_PRIORITY's own absence already establishes.
CLASS_KNOWN_SPELLS: dict[str, list[str]] = {
    "wizard": [
        "fire_bolt", "ray_of_frost", "magic_missile", "mage_armor", "shield", "fireball",
        "burning_hands", "misty_step", "sleep", "charm_person", "thunderwave",
        "hold_person", "web",
    ],
    "cleric": [
        "sacred_flame", "guidance", "cure_wounds", "bless", "healing_word", "spiritual_weapon",
        "inflict_wounds", "shield_of_faith", "guiding_bolt", "hold_person",
    ],
}

# A lightweight session-zero choice (Session.content_preference,
# server/state.py) - "standard" is deliberately absent here, needing no
# extra instruction since WorldBible's own tone_guidance (server/lore)
# already covers it; only a real, explicit choice to go lighter or more
# intense adds anything. Prepended to every turn's action_text while
# active (see _narrate_and_apply below), not stated once at session start
# and left to fade - the same "durable, not a one-time mention" reasoning
# WorldBible's own system-prompt placement already established, applied
# here at the per-turn level since this is session-scoped rather than
# something the narrator's shared system prompt can hold (one server
# process can host multiple sessions with different choices).
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

# The SRD's own real Standard Array (Basic Rules character-creation
# option), not an invented spread - same "use the official SRD numbers,
# don't make one up" convention this file's XP-per-CR/XP-per-level tables
# already follow.
STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

# Deliberately hand-written per class, not derived from a formula - same
# style CLASS_STARTING_EQUIPMENT already uses. Corrected 2026-08-11: this
# comment used to claim each class's own first *two* entries were exactly
# its SRD saving_throws - checked directly against server/rules/srd.json
# while building CLASS_SAVING_THROW_PROFICIENCIES (below) and found that
# was only ever true for fighter (Str, Con). CON was placed second for
# every class here as a universal survival-stat priority pick, not
# because it's a real saving-throw proficiency for wizard/rogue/cleric
# (their real SRD saving throws are Int+Wis, Dex+Int, and Wis+Cha - none
# include Con) - this table is for Standard Array assignment priority
# only, never used for real saving-throw proficiency, which now has its
# own separately, correctly authored table. Only the class's own *first*
# entry reliably matches its real primary saving throw; the remaining
# four are ordered by ordinary class-archetype priority (a caster wants
# its remaining physical stat over its remaining mental one, etc.). A
# blank/unrecognized class has no entry here and gets no stats at all -
# the same fallback build_starting_character's HP/inventory already use.
CLASS_ABILITY_PRIORITY: dict[str, tuple[str, ...]] = {
    "fighter": ("str", "con", "dex", "wis", "cha", "int"),
    "wizard": ("int", "con", "dex", "wis", "cha", "str"),
    "rogue": ("dex", "con", "int", "wis", "cha", "str"),
    "cleric": ("wis", "con", "str", "dex", "cha", "int"),
}

# Deterministic per-class skill proficiencies, the same "no player-chosen
# allocation yet" approach CLASS_ABILITY_PRIORITY/_generate_stats already
# take for ability scores - real 5e actually lets a player choose (2 for
# most classes, 4 for rogue) from a class's own longer list; this picks a
# fixed, thematically sensible subset from that real list rather than
# modeling the choice itself, the same simplification STANDARD_ARRAY's
# fixed assignment already makes for ability scores. A blank/unrecognized
# class has no entry and is proficient in nothing, the same fallback
# CLASS_ABILITY_PRIORITY's own absence already produces for stats.
CLASS_SKILL_PROFICIENCIES: dict[str, tuple[str, ...]] = {
    "fighter": ("athletics", "perception"),
    "wizard": ("arcana", "investigation"),
    "rogue": ("stealth", "sleight_of_hand", "perception", "deception"),
    "cleric": ("insight", "religion"),
}

# Real 5e's own fixed pair of proficient saving-throw abilities per class
# (the SRD's own "saving_throws" field, server/rules/srd.json - e.g.
# fighter's "Strength, Constitution") - verified directly against that
# data while writing this, not assumed from CLASS_ABILITY_PRIORITY above,
# whose own CON-second convention only happens to match for fighter (see
# that constant's own corrected comment). `proficiency_bonus` applies to
# a saving throw only when its ability is one of these two, real 5e's own
# rule - previously not modeled at all (request_roll's `roll_kind ==
# "save"` case got no proficiency consideration whatsoever, a real,
# previously-named gap - see ROADMAP.md). A blank/unrecognized class has
# no entry and is proficient in no saves, the same fallback
# CLASS_SKILL_PROFICIENCIES' own absence already produces.
CLASS_SAVING_THROW_PROFICIENCIES: dict[str, tuple[str, str]] = {
    "fighter": ("str", "con"),
    "wizard": ("int", "wis"),
    "rogue": ("dex", "int"),
    "cleric": ("wis", "cha"),
}

# Real 5e's own baseline Ability Score Improvement levels - the SRD's
# standard progression every class shares. Some subclasses grant extra
# ASIs (Fighter's own 6/14, in the full rules) - not modeled, since
# Oracle has no subclass system at all, the same "no ability-score/CON
# system yet" class of simplification the rest of this project already
# names rather than hides.
ASI_LEVELS = frozenset({4, 8, 12, 16, 19})


def _apply_ability_score_improvements(
    character: CharacterSheet, old_level: int, new_level: int
) -> list[str]:
    """Applies a real ASI (+2 to one ability, capped at 20 - real 5e's own
    hard ceiling) for every ASI level actually crossed between old_level
    (exclusive) and new_level (inclusive) - a loop, not a single check,
    the same "one big XP award can cross more than one threshold" reasoning
    CharacterSheet.gain_xp()'s own level-up loop already follows for HP.

    Deterministic, not a player choice - real 5e's other ASI option
    (a feat instead) isn't modeled either, since Oracle has no feat system
    at all. Always targets the class's own top CLASS_ABILITY_PRIORITY
    entry, the same "no player-chosen allocation yet" approach
    _generate_stats already uses for the initial array - falls through to
    the next-priority ability if the top one is already capped, rather
    than wasting a real improvement outright silently. Returns the ability
    key(s) actually improved, in order (empty if no ASI level was crossed,
    or a blank/unrecognized class has no priority order to draw from)."""
    priority = CLASS_ABILITY_PRIORITY.get(character.character_class.strip().lower(), ())
    if not priority or not character.stats:
        return []
    improved: list[str] = []
    for level in range(old_level + 1, new_level + 1):
        if level not in ASI_LEVELS:
            continue
        for ability in priority:
            if character.stats.get(ability, 0) < 20:
                character.stats[ability] = min(20, character.stats[ability] + 2)
                improved.append(ability)
                break
    return improved


def _asi_announcement(name: str, asi_abilities: list[str]) -> str:
    """Builds the "X's STR increases!" (or "STR and CON increase!") text
    shared by apply_update's own tool_result and the real player-facing
    system_message broadcast, so the two can't drift apart. Deduplicates
    first - crossing two ASI levels in one large XP award (rare, but
    possible) can improve the same ability twice; a real, deliberately
    small simplification, this doesn't spell out "STR increases by 4"
    for that case, just names the ability once - the sheet's own real
    number is the actual source of truth, this is a narrative nudge."""
    if not asi_abilities:
        return ""
    unique = list(dict.fromkeys(asi_abilities))
    labels = " and ".join(a.upper() for a in unique)
    verb = "increases" if len(unique) == 1 else "increase"
    return f" {name}'s {labels} {verb}!"


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


def _cast_spell(character: CharacterSheet, spell_name: str, rules: RulesIndex) -> tuple[str, bool]:
    """Applies update_character's new cast_spell field - deterministic
    slot bookkeeping (real 5e's own resource), not something the DM has
    to compute or track itself. Returns (message, changed) - changed is
    False whenever nothing was actually spent (an unknown spell, one this
    character doesn't know, or no slot left), the same "only broadcast on
    a real change" rule every other sheet mutation here already follows.

    Only ever touches known_spells/spell_slots - never validates or
    resolves a spell's actual in-fiction effect (damage, healing,
    conditions), the same "the engine resolves real data, doesn't model
    every unique effect" scope weapon/skill already keep. The DM still
    narrates the effect and, if one is warranted, applies it through this
    same update_character call's other fields (hp_delta, add_condition,
    ...) or a following request_roll - cast_spell only ever answers "was
    a real slot spent."""
    entry = rules.get_entry("spell", spell_name)
    if entry is None:
        return f"no known spell '{spell_name}'.", False

    spell_slug = slug(entry["name"])
    if spell_slug not in character.known_spells:
        return f"{character.name} doesn't know {entry['name']}.", False

    spell_level = entry.get("level", 0)
    if spell_level == 0:
        # A cantrip - unlimited use, real 5e's own rule, no slot to spend.
        return f"casts {entry['name']} (cantrip).", False

    slot_key = str(spell_level)
    if character.spell_slots.get(slot_key, 0) <= 0:
        return f"no level {spell_level} spell slots remaining - can't cast {entry['name']}.", False

    character.spell_slots[slot_key] -= 1
    remaining = character.spell_slots[slot_key]
    return f"casts {entry['name']} (level {spell_level} slot, {remaining} remaining).", True


def _generate_stats(character_class: str, stat_priority: tuple[str, ...] | None = None) -> dict[str, int]:
    """Assigns the SRD's real Standard Array to an ability priority order -
    deterministic (the same inputs always produce the same array), matching
    this project's existing "no ability-score system should depend on
    chance" stance nowhere written down but implied by every other
    deterministic mechanic here (XP awards, level-1 HP).

    stat_priority, when given, is a player's own explicit override (welcome-
    screen join payload's "stat_priority" - see _on_join_session) - the
    "broader stats survey" the original brainstorm asked for, beyond just a
    recommended class: a player who wants a str-primary rogue instead of
    the class's own dex-primary default can now say so directly. Falls back
    to the class's own CLASS_ABILITY_PRIORITY when absent or invalid (not
    exactly the 6 real ability keys, each exactly once) - the same graceful-
    miss convention every other name-based field in this file already
    follows, rather than a ValidationError on a malformed payload."""
    if stat_priority is not None and set(stat_priority) == set(ABILITY_KEYS) and len(stat_priority) == len(ABILITY_KEYS):
        priority = stat_priority
    else:
        priority = CLASS_ABILITY_PRIORITY.get(character_class.strip().lower())
    if priority is None:
        return {}
    return dict(zip(priority, STANDARD_ARRAY))


def _apply_race_bonus(stats: dict[str, int], race_entry: dict | None) -> dict[str, int]:
    """Applies a race's real ability_score_increase (server/rules/srd.json,
    e.g. dwarf's +2 con) additively on top of the class-priority Standard
    Array assignment above - real 5e stacks a racial bonus on whatever base
    array a class/priority produced, it never replaces or reorders it. A
    no-op when stats is empty (a blank/unrecognized class - see
    build_starting_character, which never has stats to add a bonus onto)
    or race_entry is None (blank/unrecognized race), the same graceful-miss
    convention every other name-based SRD lookup here already follows."""
    if not stats or not race_entry:
        return stats
    bonus = race_entry.get("ability_score_increase") or {}
    return {key: value + bonus.get(key, 0) for key, value in stats.items()}


def _parse_armor_ac(ac_text: str) -> tuple[int, int | None, bool]:
    """Parses a real SRD armor entry's own `ac` field into
    (base, dex_cap, heavy). Real 5e has three distinct shapes, all present
    in srd.json's expanded equipment table (the "Structured Equipment"
    entry, ROADMAP.md, first only had light armor - this handles all
    three now):
      - Light armor: "11 + Dex modifier" - the full, uncapped Dex modifier
        applies, positive or negative. dex_cap is None, heavy is False.
      - Medium armor: "14 + Dex modifier (max 2)" - dex_cap is the real
        integer cap (2). Real 5e RAW only caps the *positive* side - a
        negative Dex modifier still applies in full, it isn't further
        capped at 0 - so the caller must clamp with min(), not treat this
        as a hard floor.
      - Heavy armor: a bare number with no "Dex modifier" text at all
        (e.g. "18") - heavy is True, meaning Dex contributes exactly 0
        regardless of sign. This needs its own boolean, not a dex_cap of
        0 - min(dex_modifier, 0) would still apply a *negative* modifier
        as a penalty, which isn't how heavy armor actually works.
    `None` base for anything this can't parse - the same graceful-fallback
    signal `_compute_ac` already treats as "not real armor data"."""
    base_match = re.match(r"(\d+)", ac_text)
    if not base_match:
        return 10, None, False
    base = int(base_match.group(1))
    if "Dex modifier" not in ac_text:
        return base, None, True
    cap_match = re.search(r"max\s*(\d+)", ac_text)
    return base, (int(cap_match.group(1)) if cap_match else None), False


def _compute_ac(
    equipped_armor: str | None,
    dex_modifier: int,
    rules: RulesIndex,
    equipped_shield: str | None = None,
    armor_magic_bonus: int = 0,
    shield_magic_bonus: int = 0,
) -> int:
    """Real 5e's own formula: 10 (unarmored) + DEX modifier, or the
    specific equipped armor's own base AC + a real DEX contribution that
    depends on the armor's own weight class (see _parse_armor_ac: none
    capped for light, capped at a real max for medium, none at all for
    heavy) - plus a shield's own flat `ac_bonus` (server/rules/srd.json),
    additive on top of that base+Dex result rather than a replacement
    value the way equipped_armor's own `ac` field is. Takes single
    equipped_* names, not the whole inventory - only what a character
    actually has equipped affects AC, not everything they're carrying.
    Unrecognized/blank equipped_armor/equipped_shield falls back to no
    contribution, the same graceful-miss convention every other
    name-based SRD lookup here already follows.

    armor_magic_bonus/shield_magic_bonus are each equipped item's own
    real InventoryItem.magic_bonus (server/state.py, the structured-items
    feature) - callers resolve these from the acting character's own
    inventory (CharacterSheet.find_item) before calling, since this
    function only ever sees names, not the character. Additive on top of
    the SRD base stats the same way a shield's ac_bonus already is - a
    +1 suit of armor is still whatever armor it is, plus 1."""
    base = 10
    dex_cap: int | None = None
    heavy = False
    if equipped_armor:
        entry = rules.get_entry("equipment", equipped_armor)
        ac_text = entry.get("ac") if entry is not None else None
        if ac_text:
            base, dex_cap, heavy = _parse_armor_ac(ac_text)
    if heavy:
        effective_dex = 0
    elif dex_cap is None:
        effective_dex = dex_modifier
    else:
        effective_dex = min(dex_modifier, dex_cap)
    shield_bonus = 0
    if equipped_shield:
        shield_entry = rules.get_entry("equipment", equipped_shield)
        if shield_entry is not None:
            shield_bonus = shield_entry.get("ac_bonus") or 0
    return base + effective_dex + armor_magic_bonus + shield_bonus + shield_magic_bonus


def _auto_equip_starting_gear(
    inventory: list[InventoryItem], rules: RulesIndex
) -> tuple[str | None, str | None, str | None]:
    """Picks the first weapon-like, armor-like, and shield-like item out of
    a fresh character's starting inventory (CLASS_STARTING_EQUIPMENT) to
    equip automatically - real tabletop chargen starts you already
    wielding/wearing your starting gear, not carrying it unequipped until
    a player remembers to run /equip. A weapon is any SRD equipment entry
    with a `damage` field, armor any entry with an `ac` field, a shield any
    entry with an `ac_bonus` field - the same distinction _parse_armor_ac/
    _compute_ac already draw for AC, generalized to also recognize weapons
    rather than hardcoding "the second item is armor". No current class
    starts with a shield (CLASS_STARTING_EQUIPMENT), so this is untested
    by real starting-kit data yet - included for the same completeness
    reason weapon/armor detection isn't hardcoded to "exactly 2 items"."""
    weapon: str | None = None
    armor: str | None = None
    shield: str | None = None
    for item in inventory:
        entry = rules.get_entry("equipment", item.name)
        if entry is None:
            continue
        if weapon is None and entry.get("damage"):
            weapon = item.name
        elif armor is None and entry.get("ac"):
            armor = item.name
        elif shield is None and entry.get("ac_bonus"):
            shield = item.name
    return weapon, armor, shield


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
        # Same "fluff, not bookkeeping" treatment character_class already
        # gets - another player's race is real, visible-at-the-table
        # information (like a name or class), not private state the way
        # inventory/stats/notes are.
        "race": character.race,
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


def _class_features_for(class_entry: dict | None, level: int) -> list[str]:
    """Every class feature a character has earned through `level` -
    srd.json's own level_1_features plus each features_by_level entry at
    every later level, accumulated in order. Derived from (class, level)
    on every view build rather than stored on the sheet: the same real
    data always produces the same features, so there's no state to
    persist or migrate and a level-up automatically reveals what it
    granted. Levels whose only content is a subclass choice or an ASI
    have no entry in srd.json - ASI math is already applied separately,
    and subclasses are deliberately out of scope (same call the race
    system made about subraces)."""
    if class_entry is None:
        return []
    feats = list(class_entry.get("level_1_features", []))
    by_level = class_entry.get("features_by_level", {})
    for lvl in range(2, level + 1):
        feats.extend(by_level.get(str(lvl), []))
    return feats


NPC_NOTES_CONTEXT_MAX_CHARS = 80


def _npc_roster(session: Session) -> str:
    """One bounded line per living tracked NPC, appended to the DM's
    world_summary so dispositions/notes/wounds stay visible even after the
    NPC has scrolled out of the rolling history window - the structured
    subset of ROADMAP.md item 1's memory blind spot, cheap because it's
    real data rather than prose needing a summary. Without this,
    `disposition`'s own stated purpose ("stay consistent against turn to
    turn") only ever worked while the NPC was still in recent history.
    Dead NPCs are excluded - gone from active play. "" when there's
    nothing living to report, the same "don't render the absent default"
    convention WorldState.narrator_context() follows.
    # ponytail: no cap on tracked-NPC count; a session that introduces
    dozens of NPCs would grow this block linearly - trim by recency if
    that's ever observed in play."""
    lines = []
    for npc in session.npcs.values():
        if npc.hp <= 0:
            continue
        bits = [f"HP {npc.hp}/{npc.max_hp}"]
        if npc.disposition != "neutral":
            bits.append(npc.disposition)
        if npc.conditions:
            bits.append(", ".join(sorted(npc.conditions)))
        line = f"- {npc.name}: " + ", ".join(bits)
        if npc.notes:
            line += f" - {npc.notes[:NPC_NOTES_CONTEXT_MAX_CHARS]}"
        lines.append(line)
    if not lines:
        return ""
    return "Tracked NPCs:\n" + "\n".join(lines)


def _owner_character_view(character: CharacterSheet, rules: RulesIndex) -> dict:
    """The owner's own full sheet - everything model_dump() already has,
    plus two fields that exist but were never actually sent: real class
    features (_class_features_for, above: srd.json's own per-class feature
    text accumulated through the character's current level, so a level-up
    automatically adds what it granted) and a persistent skill-
    proficiency list (CLASS_SKILL_PROFICIENCIES, which already drives real
    roll bonuses but previously only ever showed up transiently in a
    roll's own label text, never as something a player could just look
    at). Built for the tabbed character sheet UI (ROADMAP.md item 7) -
    backs both _state_sync_envelope's and _character_update_envelope's
    owner-only payloads, the same "one place defines the shape" reasoning
    _public_character_view already follows for the public side."""
    class_entry = rules.get_entry("class", character.character_class)
    race_entry = rules.get_entry("race", character.race) if character.race else None
    return {
        **character.model_dump(),
        "class_features": _class_features_for(class_entry, character.level),
        "racial_traits": list((race_entry or {}).get("traits", [])),
        "skill_proficiencies": list(
            CLASS_SKILL_PROFICIENCIES.get(character.character_class.strip().lower(), ())
        ),
    }


def _dice_roll_tags(roll: dict) -> str:
    """The shared descriptive tag suffix for a roll - damage type, a
    carried weapon's real magic bonus, ability modifier, skill/spell
    proficiency, roll kind, and any tracked-condition disadvantage -
    everything that explains *why* a roll's total is what it is, beyond
    the bare dice notation. `roll` is the same dict shape `request_roll`
    (below) already appends to `rolls_made` and `_dice_result_envelope`
    already reads from, so any field this needs is already present by
    the time either caller runs.

    Used identically by request_roll's own DM-facing tool_result text and
    GameEngine._dice_log_text's broadcast log line - previously two
    independent copies of this exact logic that had already drifted out
    of sync once (weapon_magic_bonus landed in one but not the other - a
    real bug found while building the structured-items feature, ROADMAP.md
    item 14). One shared function means that class of bug can't recur.
    Deliberately doesn't include `reason`/`purpose` or the critical-hit
    callout - those two are real, deliberate differences between the two
    callers (the DM already knows why it asked for the roll, so its own
    tool_result never echoes `reason` back; `_dice_log_text` does, since
    the player has no other way to know it) rather than something to
    unify away."""
    damage_type = roll.get("damage_type")
    damage_label = f" ({damage_type})" if damage_type else ""
    weapon_magic_bonus = roll.get("weapon_magic_bonus")
    weapon_magic_label = f" +{weapon_magic_bonus} magic" if weapon_magic_bonus else ""
    ability_mod = roll.get("ability_modifier")
    ability_label = f" +{ability_mod} {roll['ability'].upper()}" if ability_mod is not None else ""
    skill = roll.get("skill")
    skill_label = ""
    if skill:
        skill_label = f" ({skill.replace('_', ' ').title()}"
        skill_label += f", +{roll['proficiency_bonus']} proficiency)" if roll.get("proficient") else ")"
    spell = roll.get("spell")
    spell_label = f" ({spell}, +{roll['proficiency_bonus']} proficiency)" if spell else ""
    # A save is the one roll_kind that can carry real proficiency
    # (CLASS_SAVING_THROW_PROFICIENCIES) with no skill/spell label of its
    # own to show it on - skill/spell already cover themselves above, so
    # this only adds the tag when neither did.
    roll_kind = roll.get("roll_kind")
    if roll_kind == "save" and roll.get("proficient") and not skill_label and not spell_label:
        roll_kind_label = f" ({roll_kind}, +{roll['proficiency_bonus']} proficiency)"
    else:
        roll_kind_label = f" ({roll_kind})" if roll_kind else ""
    disadvantage_reasons = roll.get("disadvantage_reasons")
    disadvantage_label = f" (disadvantage: {', '.join(disadvantage_reasons)})" if disadvantage_reasons else ""
    return (
        damage_label + weapon_magic_label + ability_label + skill_label + spell_label
        + roll_kind_label + disadvantage_label
    )


def _outcome_category(update: dict) -> str | None:
    """Picks a single dominant category for a real update_character change,
    so the client can color-code the resulting log line by what actually
    happened - a direct owner ask for damage/heal/spell/item to read
    differently at a glance, not all blend into the same plain text. Takes
    priority when a call combines several (e.g. a poisoned dart: hp_delta
    and add_condition in one call) since damage/heal is the most
    narratively dominant outcome. None means nothing worth a dedicated
    color (e.g. only notes/disposition changed) - the same "not every
    change needs a spotlight" restraint _npc_status_line's own dim
    default already applies."""
    hp_delta = update.get("hp_delta")
    if hp_delta:
        return "damage" if hp_delta < 0 else "heal"
    if update.get("rest"):
        return "heal"
    if update.get("add_condition") or update.get("remove_condition"):
        return "condition"
    if update.get("cast_spell"):
        return "spell"
    if update.get("add_item") or update.get("remove_item"):
        return "item"
    return None


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
    player_id: str,
    name: str,
    character_class: str,
    rules: RulesIndex,
    origin_table: OriginTable | None = None,
    stat_priority: tuple[str, ...] | None = None,
    race: str = "",
) -> CharacterSheet:
    """Builds a real starting sheet from a chosen class via the SRD data,
    or falls back to the original blank hp=10/max_hp=10 sheet for a blank
    or unrecognized class - keeps old clients/tests that don't send
    character_class at all working unchanged.

    Every new character gets a random pre-Aetherfall origin (server/lore's
    random_origin) regardless of class choice - the near-death/transport
    premise applies to everyone, not just characters who picked a real
    class.

    stat_priority is a player's own optional override of which ability
    gets which Standard Array slot (see _generate_stats) - ignored
    entirely for a blank/unrecognized class, the same way the class's own
    equipment/spells are.

    race is a genuinely independent choice from character_class - a
    blank/unrecognized value degrades the same graceful way (no ability
    bonus, no racial traits) rather than blocking creation, and is still
    recorded even for a classless character, since race and class don't
    depend on each other."""
    background = random_origin(origin_table or load_default_origin_table()).sheet_summary()

    race_entry = rules.get_entry("race", race) if race else None
    race_name = race_entry["name"] if race_entry else ""

    class_entry = rules.get_entry("class", character_class) if character_class else None
    if class_entry is None:
        return CharacterSheet(
            player_id=player_id, name=name, hp=10, max_hp=10, background=background, race=race_name
        )

    stats = _apply_race_bonus(_generate_stats(character_class, stat_priority), race_entry)
    con_mod = ability_modifier(stats["con"]) if stats else 0
    # Real 5e's level-1 HP formula: hit die max + CON modifier, floored at
    # 1 (a character can't start with 0 or negative HP even from a bad
    # CON score) - the CON-modifier half of the "no ability-score/CON
    # system yet" gap this file used to flag is closed by this line.
    max_hp = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod)
    inventory = [
        InventoryItem(name=item_name)
        for item_name in CLASS_STARTING_EQUIPMENT.get(character_class.strip().lower(), [])
    ]
    dex_mod = ability_modifier(stats["dex"]) if stats else 0
    known_spells = list(CLASS_KNOWN_SPELLS.get(character_class.strip().lower(), []))
    spell_slots = rules.spell_slots_by_level(1) if known_spells else {}
    equipped_weapon, equipped_armor, equipped_shield = _auto_equip_starting_gear(inventory, rules)
    return CharacterSheet(
        player_id=player_id,
        name=name,
        hp=max_hp,
        max_hp=max_hp,
        character_class=class_entry["name"],
        race=race_name,
        stats=stats,
        inventory=inventory,
        equipped_weapon=equipped_weapon,
        equipped_armor=equipped_armor,
        equipped_shield=equipped_shield,
        ac=_compute_ac(equipped_armor, dex_mod, rules, equipped_shield),
        known_spells=known_spells,
        spell_slots=dict(spell_slots),
        max_spell_slots=dict(spell_slots),
        background=background,
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
        world_bible: WorldBible | None = None,
        origin_table: OriginTable | None = None,
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store
        self._enable_opening_scene = enable_opening_scene
        self._rules = rules or RulesIndex.load_default()
        # Composes the opening scene's action_text below (see
        # _on_start_session) - the near-death/transport/Guardian-greeting
        # premise, with real setting facts rather than left for the DM to
        # invent freely. Same "load once, default to the bundled one"
        # precedent self._rules above already establishes.
        self._world_bible = world_bible or load_default_world_bible()
        # Feeds build_starting_character's random per-character origin
        # (background/trait/near-death) - same load-once precedent.
        self._origin_table = origin_table or load_default_origin_table()
        self._pending_proposals: dict[str, dict] = {}
        # World-context lorebook (docs/protocol.md "Protocol v2 additions -
        # World context -> lorebook"): empty until a client sends
        # context_select; rebuilt from disk on every selection change and
        # on construction when a reloaded save carries context_files.
        self._world_context_dir = Path(os.environ.get("WORLD_CONTEXT_DIR", "world_context"))
        self._lorebook = Lorebook()
        if session.context_files:
            self._rebuild_lorebook(session.context_files)

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
            # Same real formula as level-1 HP (hit die max + CON modifier,
            # floored at 1 per level) - a character with a negative CON
            # modifier still gains at least 1 HP per level, never 0 or
            # negative growth.
            con_mod = ability_modifier(character.stats["con"]) if character.stats else 0
            hp_gain = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod) * levels_gained
            character.max_hp += hp_gain
            character.hp += hp_gain
            # AC doesn't recompute here even when DEX is the ability
            # improved below (e.g. a rogue's ASI) - the exact same
            # already-documented "AC doesn't recompute if stats change
            # after character creation" simplification the Structured
            # Equipment entry (ROADMAP.md) already accepts for inventory
            # changes, extended to cover this too rather than treated as a
            # new, separate gap.
            asi_abilities = _apply_ability_score_improvements(character, old_level, character.level)
            # Spell slots grow by the real delta between the old and new
            # level's max, not a full reset to the new max - the same
            # "level-up grants more, it isn't a free rest" reasoning HP
            # growth above already follows, applied to a resource that can
            # also be partially spent already. A non-caster
            # (max_spell_slots already empty) sees no change, since
            # new_max is also {} for it.
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
        """Splits a kill's XP across the whole party rather than giving it
        all to whoever's turn it is - real 5e's party-wide rule, still
        applied deterministically (no DM tool call, the same reliability
        reasoning ROADMAP.md already documents for XP). The split is a floor
        division and the remainder simply drops; a solo session (N=1) awards
        everything to the one member, an exact no-op vs. the old behaviour.
        Returns (player_id, member, levels_gained, asi_abilities) per party
        member in session.characters - shared by apply_update's closure and
        _on_apply_proposed_change so the two paths can't drift."""
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

        if is_new_character:
            name = envelope.payload.get("player_name", player_id)
            character_class = envelope.payload.get("character_class", "")
            # Independent of character_class - see build_starting_character's
            # own docstring for why a blank/unrecognized value degrades
            # gracefully rather than blocking creation.
            race = envelope.payload.get("race", "")
            # A player's own optional override of the class's default
            # ability-priority order (see _generate_stats) - a list of the
            # 6 real ability keys, e.g. ["str", "con", "dex", "wis", "cha",
            # "int"]. Not validated here beyond the type/shape check -
            # _generate_stats already falls back to the class default for
            # anything that isn't exactly those 6 keys once each, the same
            # graceful-miss convention this method's own character_class
            # handling already relies on.
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
                # A real, live-found gap (ROADMAP.md's campaign dry-run,
                # 2026-08-10): a typo'd/unrecognized class string used to
                # silently fall back to a blank, classless, stat-less
                # character with zero indication anything went wrong - a
                # player could type a garbled class name and not notice
                # until well into the session. Only warns when the player
                # actually typed something that didn't match - a genuinely
                # blank field is the UI's own explicit "blank to skip"
                # option, not a mistake worth flagging.
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
                # Same silent-mistake gap the class warning above closed,
                # for the same reason - a typo'd/unrecognized race string
                # otherwise costs a player their ability bonus and racial
                # traits with no indication anything went wrong.
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
            # A reconnect (this player_id already has a character here, most
            # often a stale local .player_id file from a previous session)
            # keeps that character's existing name/class regardless of what
            # was just typed on the welcome screen - real found-live
            # confusion, not hypothetical: a player types a fresh name
            # expecting a new character, silently gets an old one back
            # with zero indication their input was ignored. A private
            # heads-up only when it'd actually be surprising - the typed
            # name genuinely differs from what they're really playing.
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

        # A private "here's where things left off" recap for anyone
        # joining a story already underway - a returning player reconnecting
        # AND a brand-new player joining an in-progress multiplayer game
        # (arguably the one who needs it most) both got nothing beyond
        # whatever their own client silently reconstructs from state_sync
        # before this - no scrolled-back log, no restated objective.
        # Skipped only when nothing has happened yet (_has_started() false
        # - a session still sitting in its pre-game lobby has no story to
        # recap). Entirely deterministic (WorldState/Session.log, already-
        # tracked data), not an LLM call - always available instantly,
        # regardless of narrator reliability or latency, the same "engine
        # composes it" discipline the opening-scene premise (server/lore)
        # already established. Sent *after* state_sync above, not before -
        # a real ordering bug caught before it ever shipped: the client
        # only transitions off WelcomeScreen once it processes state_sync
        # (client/app.py's own _handle), so a system_message arriving
        # earlier has no SessionScreen/LobbyScreen to render into yet and
        # is silently dropped - the exact race _on_start_session's own
        # session_started-before-narration ordering already guards against
        # elsewhere in this file.
        if self._has_started():
            await self._send_to(player_id, self._system_envelope(self._resume_recap(), level="info"))
            # A returning character specifically (not a brand-new player,
            # who has no own prior turns that could have scrolled out of
            # the rolling history window in the first place) gets the same
            # grounding fed to the DM itself, once, on their next real
            # action - see Session.pending_dm_recap's own docstring
            # (server/state.py) for the full "why" and _on_player_action
            # for where this actually gets consumed.
            if not is_new_character and player_id not in self._session.pending_dm_recap:
                self._session.pending_dm_recap.append(player_id)

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

        # A lightweight session-zero choice (LobbyScreen's own selector,
        # client/app.py) from whoever actually starts the adventure - an
        # unrecognized or missing value falls back to the same "standard"
        # default Session.content_preference already has, the same
        # graceful-miss convention every other name-based field in this
        # file already follows, rather than a pydantic ValidationError on
        # a malformed/adversarial payload.
        content_preference = envelope.payload.get("content_preference")
        if content_preference in ("lighter", "standard", "intense"):
            self._session.content_preference = content_preference

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
            action_text = self._world_bible.opening_scene_prompt(names, plural=True)
        else:
            action_text = self._world_bible.opening_scene_prompt(
                character.name, plural=False, origin_detail=character.background
            )

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

    async def _on_start_combat(self, envelope: Envelope) -> None:
        """Real 5e formal initiative - any joined player may trigger this
        (the same symmetric, no-host-role precedent start_session already
        establishes), not something detected from narration. Deliberately
        explicit rather than inferred: this project's own tool-call
        reliability investigation (ROADMAP.md) found the DM model can't be
        trusted to reliably notice and act on state changes on its own, and
        "did combat just start" is exactly that kind of judgment call - a
        player saying so is the one signal that's actually reliable.

        Idempotent, matching _on_start_session's own precedent - a second
        start_combat while already in combat (two players both reaching
        for it, a retry) is a silent no-op, not a re-rolled order.

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

        # Highest roll first; ties broken by the higher DEX modifier (real
        # 5e's own tiebreak), then by Python's stable sort preserving each
        # participant's original (join/introduction) order - a real,
        # deterministic tiebreak beyond that isn't attempted, matching real
        # 5e's own "DM decides" for a tie that persists past DEX.
        participants.sort(key=lambda p: (-p[1], -p[2]))

        self._session.pre_combat_turn_order = list(self._session.turn_order)
        self._session.turn_order = [pid for (_, _, _, pid) in participants if pid is not None]
        self._session.current_turn_index = 0
        self._session.in_combat = True

        order_text = ", ".join(f"{name} ({roll})" for name, roll, _, _ in participants)
        await self._broadcast(self._system_envelope(f"Combat begins! Initiative order: {order_text}.", level="info"))
        await self._broadcast(self._turn_prompt_envelope())
        await self._save()

    async def _on_end_combat(self, envelope: Envelope) -> None:
        """Symmetric with _on_start_combat - any joined player may end
        combat too, the same "no host role" precedent. Idempotent: a
        second end_combat outside combat is a silent no-op.

        Restores turn_order to pre_combat_turn_order (plain join order,
        snapshotted the moment combat began) plus anyone who joined mid-
        combat - _on_join_session appends a new joiner straight to the
        live turn_order during combat, same as it always does, so those
        joiners are simply whatever's in turn_order now but wasn't in the
        snapshot, appended in the order they actually joined."""
        if not self._session.in_combat:
            return

        pre_combat = self._session.pre_combat_turn_order or []
        latecomers = [pid for pid in self._session.turn_order if pid not in pre_combat]
        self._session.turn_order = pre_combat + latecomers
        self._session.pre_combat_turn_order = None
        self._session.current_turn_index = 0
        self._session.in_combat = False

        await self._broadcast(self._system_envelope("Combat ends.", level="info"))
        if self._session.current_turn is not None:
            await self._broadcast(self._turn_prompt_envelope())
        await self._save()

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
        self._pending_proposals.pop(player_id, None)
        await self._broadcast(self._log_envelope("action", f"{character.name}: {text}"))

        # Consumed (popped) here, not just checked - a real gap between
        # this reconnecting player's join and their first real action
        # shouldn't re-trigger on every later turn, only this one. See
        # Session.pending_dm_recap's own docstring (server/state.py) for
        # the full "why".
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
        await self._save(player_id)
        await self._broadcast(self._turn_prompt_envelope())

    async def _narrate_and_apply(
        self,
        character: CharacterSheet,
        action_text: str,
        check_for_missed_changes: bool = True,
        dm_recap: str | None = None,
    ) -> str:
        """Runs one DM narrate() call for the given character/action, wiring
        up apply_update/request_roll/update_world, broadcasting narration and
        any resulting state changes exactly as a normal turn does. Returns
        the full narration text. Shared by _on_player_action (a real turn)
        and _narrate_opening_scene (a synthetic "turn" on campaign start that
        doesn't consume the turn queue).

        dm_recap, when given (_on_player_action's own Session.pending_dm_recap
        consumption), is prepended to the DM-facing action text the same way
        CONTENT_PREFERENCE_HINTS already is - invisible to the player (never
        touches the broadcast action_text/action log line, only what the
        narrator backend itself receives)."""
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
        # (text, category) - one entry per real update_character change
        # this turn, broadcast as color-coded log lines after narration
        # finishes streaming (a direct owner ask: damage/heal/spell/item
        # should read differently at a glance, not blend into plain text
        # narration - see _outcome_category above). Same deferred-broadcast
        # shape rolls_made/xp_awards already use, for the same reason -
        # apply_update is a synchronous tool callback, so nothing here can
        # await a broadcast directly.
        outcomes: list[tuple[str, str]] = []
        # (npc_name, xp_awarded, levels_gained) - one entry per NPC this
        # turn's apply_update calls actually defeated, so a broadcast can
        # announce each defeat/level-up after narration finishes streaming,
        # not interrupt it mid-stream.
        xp_awards: list[tuple[str, int, list[tuple[str, CharacterSheet, int, list[str]]]]] = []
        xp_award_members: dict[str, CharacterSheet] = {}

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
            weapon_magic_bonus = 0
            if weapon:
                equipment_entry = self._rules.get_entry("equipment", weapon)
                weapon_damage = equipment_entry.get("damage") if equipment_entry else None
                if weapon_damage:
                    notation, _, damage_type = weapon_damage.partition(" ")
                # The acting character's own carried instance of this
                # weapon, if any - a magic weapon (InventoryItem.magic_bonus,
                # server/state.py, set via update_character's own add_item +
                # magic_bonus) adds to its damage roll, real 5e's own rule.
                # Only the damage roll, not also the to-hit roll - `weapon`
                # only ever means "resolve this weapon's real damage die"
                # (this closure's own comment above), and a to-hit roll
                # never names a weapon at all today - a real, named gap for
                # a later pass, not silently promised here.
                owned_weapon = character.find_item(weapon)
                if owned_weapon:
                    weapon_magic_bonus = owned_weapon.magic_bonus

            # spell, when given (e.g. "fire_bolt"), resolves a real
            # attack-roll-shaped cantrip/spell the same way weapon resolves
            # a physical attack - only spells with an "attack": true and a
            # structured "damage" field in srd.json set anything here (a
            # save-based spell like sacred_flame, or a non-damaging one
            # like bless, has nothing an attack roll would resolve, so this
            # is a graceful no-op for those - see "Spellcasting" in
            # docs/protocol.md for why request_roll never rolls a target's
            # saving throw on the caster's behalf). Spell name matching
            # doesn't check known_spells here (unlike cast_spell on
            # update_character) - this only ever resolves real dice/damage
            # data for display, it doesn't consume a slot or need to.
            spell = update.get("spell")
            spell_entry = self._rules.get_entry("spell", spell) if spell else None
            if spell_entry and spell_entry.get("attack") and spell_entry.get("damage"):
                notation, _, damage_type = spell_entry["damage"].partition(" ")

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

            # skill, when given (e.g. "stealth"), is real 5e's own name for
            # what's actually being checked - the engine resolves its real
            # governing ability (SKILL_ABILITIES) automatically rather than
            # asking the DM to also separately pass ability for the same
            # roll, the same "resolve real data server-side" reasoning
            # weapon already applies to damage dice. An explicit ability
            # still wins if the DM passes one anyway (a real, if rare, 5e
            # case - some rolls swap a skill's usual ability), matching the
            # same "explicit override beats an automatic default" priority
            # order _xp_for_npc's own explicit-xp-beats-CR-lookup already
            # establishes. An unrecognized skill name is a graceful no-op,
            # not an error - the same convention every other name-based
            # lookup here already follows.
            skill = update.get("skill")
            if skill not in SKILL_ABILITIES:
                skill = None
            if skill and not ability:
                ability = SKILL_ABILITIES[skill]

            # A resolved spell (see above) auto-fills ability from the
            # character's own real spellcasting ability (SPELLCASTING_ABILITY)
            # the same way skill does, when the DM didn't already give one.
            if spell_entry and spell_entry.get("attack") and not ability:
                ability = SPELLCASTING_ABILITY.get(character.character_class.strip().lower())

            ability_mod = character.stat_modifiers.get(ability) if ability else None

            # Proficiency bonus - real 5e's own level-scaled bonus
            # (CharacterSheet.proficiency_bonus, a computed field). Three
            # different rules for when it applies, all real 5e: a skill
            # check only gets it if the character happens to be proficient
            # in that specific skill (CLASS_SKILL_PROFICIENCIES); a spell
            # attack always gets it (5e never lets a caster be "not
            # proficient" with their own spells); a saving throw only gets
            # it if its ability is one of the character's class's own two
            # proficient saves (CLASS_SAVING_THROW_PROFICIENCIES) - three
            # real, deliberately different rules, not an inconsistency.
            # Fully automatic either way, the same "the engine computes
            # this from real tracked state" reasoning disadvantage/XP/ASI
            # already follow - the DM never has to know or track
            # proficiencies.
            proficient = bool(skill) and skill in CLASS_SKILL_PROFICIENCIES.get(
                character.character_class.strip().lower(), ()
            )
            proficiency_bonus = character.proficiency_bonus if proficient else 0
            if spell_entry and spell_entry.get("attack"):
                proficient = True
                proficiency_bonus = character.proficiency_bonus
            # update.get("roll_kind"), not the local roll_kind variable -
            # that's only (re)computed further below, and only ever
            # inferred for "check"/"attack", never "save" (a save has no
            # equivalent auto-detectable signal the way skill/spell-attack
            # do), so reading the raw input here is correct, not a race.
            if update.get("roll_kind") == "save" and ability in CLASS_SAVING_THROW_PROFICIENCIES.get(
                character.character_class.strip().lower(), ()
            ):
                proficient = True
                proficiency_bonus = character.proficiency_bonus

            # roll_kind ("attack"/"save"/"check") is purely descriptive of
            # what the roll represents - unlike every other request_roll
            # field, it never changes the roll's own math (no modifier, no
            # notation change). Its one real effect is narrowing which
            # tracked conditions apply disadvantage below (real 5e's own
            # per-roll-type scoping - see ROLL_KIND_DISADVANTAGE_EXCLUSIONS).
            # An unrecognized value is treated the same as omitted (None) -
            # the same graceful-miss convention every other name-based
            # field in this closure (weapon, ability) already follows,
            # rather than erroring on a value the model got slightly wrong.
            # A skill check is, definitionally, a "check"; a resolved spell
            # attack is, definitionally, an "attack" - defaulting either
            # here (only when the DM didn't already say otherwise) means
            # naming one also gets the real per-condition disadvantage
            # scoping for free, without the DM needing to pass two
            # redundant fields for the same underlying fact.
            roll_kind = update.get("roll_kind")
            if roll_kind not in ("attack", "save", "check"):
                if skill:
                    roll_kind = "check"
                elif spell_entry and spell_entry.get("attack"):
                    roll_kind = "attack"
                else:
                    roll_kind = None

            # Fully automatic, never a model-supplied field - the same
            # "the engine computes this from real tracked state, not the
            # model's judgment" reasoning every other mechanic in this
            # file already follows. A character narrating their way into
            # a disadvantageous circumstance the engine has no tracked
            # state for (fighting in darkness, an ally in the way) still
            # isn't modeled - real, deliberate future work, not silently
            # promised here.
            disadvantage_reasons = _has_disadvantage(character, roll_kind)
            disadvantage = bool(disadvantage_reasons)

            try:
                total, rolls, sides = dice.roll(
                    notation,
                    extra_modifier=(ability_mod or 0) + proficiency_bonus + weapon_magic_bonus,
                    disadvantage=disadvantage,
                )
            except dice.InvalidDiceNotation as exc:
                return f"Invalid dice notation: {exc}"

            success = None if dc is None else total >= dc

            # A critical hit - real 5e's own natural-20-on-an-attack-roll
            # rule. kept_roll mirrors the client's own disadvantage-
            # narrowing logic (_dice_result_line): under disadvantage,
            # rolls holds both d20s but only the worse was actually kept,
            # so checking either raw entry could wrongly call a discarded
            # 20 a crit. advantage is never actually set anywhere in this
            # codebase today (only disadvantage, from tracked conditions),
            # so rolls always has exactly one entry outside the
            # disadvantage case. Deliberately just an announced fact for
            # now, not automatic damage-doubling - that needs the engine
            # to correlate this roll with a later, separate damage roll
            # for the same attack, a bigger mechanic than detecting the
            # crit itself (see ROADMAP.md).
            kept_roll = min(rolls) if disadvantage else rolls[0]
            critical = roll_kind == "attack" and sides == 20 and kept_roll == 20

            roll_entry = {
                "dice": notation, "total": total, "rolls": rolls, "sides": sides,
                "dc": dc, "success": success, "reason": reason,
                "ability": ability, "ability_modifier": ability_mod,
                "damage_type": damage_type, "roll_kind": roll_kind,
                "skill": skill, "proficient": proficient, "proficiency_bonus": proficiency_bonus,
                "spell": spell_entry["name"] if spell_entry and spell_entry.get("attack") else None,
                "disadvantage": disadvantage, "disadvantage_reasons": disadvantage_reasons,
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

            # A model given the character sheet as JSON (which includes its own
            # player_id, name, and conditions) sometimes echoes one of those
            # back as target instead of "self" - without this, that misroutes
            # into the NPC branch below and silently creates a phantom NPC
            # sheet named after the player's own id, name, or (found live
            # during a campaign dry-run, ROADMAP.md, 2026-08-10) one of their
            # own already-applied conditions (e.g. "Veil-Touched", every
            # character's own origin condition) - a mistargeted hit on the
            # acting character routes here as a normal self-update instead of
            # spawning a bogus NPC, the same resolution this exact class of
            # mistargeting already gets for player_id/name confusion. Scoped
            # to the acting character's own *current* conditions, not a fixed
            # list - the coincidence of an unrelated real NPC sharing a name
            # with a condition string is negligible, but a static list would
            # need to know every condition any origin/narration could ever
            # apply.
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

            if changed:
                category = _outcome_category(update)
                if category:
                    outcomes.append((f"{npc.name}: {delta_result}", category))

            xp_note = ""
            if defeated:
                # Split across the whole party (real 5e's rule) by
                # _award_party_xp - a floor division, the remainder simply
                # drops, and a solo session is an exact no-op that awards
                # everything to the one member.
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

        # Scene-facts snapshot for this turn (docs/protocol.md "Protocol v2
        # additions - Scene envelope"): the narrator's decide phase reports
        # them through the scene_sink closure below; broadcast after narration
        # finishes so the client renders them as the turn's resolution.
        scene_facts: dict = {}

        def scene_sink(facts: dict) -> None:
            nonlocal scene_facts
            # The 4-action cap is a protocol guarantee (docs/protocol.md),
            # enforced here server-side rather than trusted to each backend.
            trimmed = dict(facts)
            trimmed["suggested_actions"] = list(facts.get("suggested_actions", []))[:4]
            scene_facts = trimmed

        full_clocks_before = {c.name for c in self._session.world.clocks if c.filled >= c.segments}
        # (text) - one entry per progress clock this turn's update_world
        # calls filled to its last segment (docs/protocol.md "Clocks"),
        # announced as a system_message after narration finishes streaming,
        # the same deferred-broadcast shape outcomes/xp_awards already use -
        # apply_update/update_world are synchronous tool callbacks, so
        # nothing here can await a broadcast directly.
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

        # Prepended here rather than baked into the DM's system prompt -
        # content_preference is per-session (Session.content_preference,
        # server/state.py) while the system prompt is shared/global across
        # every session one server process hosts. "standard" has no entry
        # in CONTENT_PREFERENCE_HINTS above, so this is a no-op for the
        # common/default case. Prepending to the real action_text used in
        # this narrate() call (not the raw text broadcast to the action
        # log by _on_player_action, which happens before this method is
        # even called) keeps the hint invisible to players.
        hint = CONTENT_PREFERENCE_HINTS.get(self._session.content_preference)
        narrate_action_text = f"[{hint}]\n{action_text}" if hint else action_text
        # dm_recap (this reconnecting player's own pending_dm_recap, popped
        # by _on_player_action) goes in front of the content-preference
        # hint - context for "what's going on" belongs before a tone
        # instruction, not after it.
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
        # keyword hits from the recent play window under a character budget,
        # appended to the same grounding region of the prompt world state
        # and the NPC roster already occupy. Empty selection -> empty block,
        # so sessions that never touch world_context are byte-identical to
        # before.
        window_text = "\n".join(
            str(message.get("content", "")) for message in self._session.history[-6:]
        )
        lore_block = self._lorebook.injection_block(f"{window_text}\n{npc_roster}\n{world_summary}")
        if not lore_block:
            lore_block = self._lorebook.injection_block(narrate_action_text)
        if lore_block:
            world_summary = f"{world_summary}\n\n{lore_block}" if world_summary else lore_block
        async for chunk in self._dm.narrate(
            history=self._session.history,
            character_summary=character.model_dump_json(),
            action_text=narrate_action_text,
            apply_update=apply_update,
            request_roll=request_roll,
            update_world=update_world,
            world_summary=world_summary,
            **(
                # Optional-capability convention (same getattr pattern
                # check_missed_change uses): only backends that decide scene
                # facts accept scene_sink - test doubles and legacy paths
                # keep their exact narrate() signature untouched.
                {"scene_sink": scene_sink}
                if getattr(self._dm, "supports_scene_facts", False)
                else {}
            ),
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

        # A real chance for the DM to self-correct, not just a passive
        # warning - see check_for_missed_changes's own comment further
        # below for the full "why". Deliberately placed here, before the
        # sheet_changed/npcs_touched/outcomes broadcasts below, rather than
        # alongside the warning itself: a correction applied via this call
        # mutates that same nonlocal state through apply_update exactly
        # like a normal in-turn tool call would, so it needs to happen
        # before those broadcasts run to be picked up by them, not after.
        # getattr, not a required Protocol method - most test doubles
        # (StubDM and friends, tests/test_engine.py) have no need to
        # implement this, the same "optional capability" convention this
        # project already uses for request_roll/update_world being None.
        # No POSSIBLE_UNTRACKED_CHANGE_PATTERN gate here (unlike the passive
        # warning below) - that regex is deliberately narrow (its own
        # comment admits real false negatives on phrasing it doesn't catch),
        # and a clean check_missed_change() response costs nothing beyond
        # one extra structured-output call: _has_outcome_change() gates the
        # actual apply_update, so a quiet turn just gets an honest "nothing
        # to fix" rather than a spurious correction. The warning tier below
        # stays regex-gated - it's player-facing, so false positives there
        # cost attention, not just latency.
        missed_change_corrected = False
        if check_for_missed_changes and not sheet_changed and not npcs_touched:
            check_missed_change = getattr(self._dm, "check_missed_change", None)
            if check_missed_change is not None:
                missed_change_corrected = await check_missed_change(buffer, character.model_dump_json(), apply_update)

        for roll in rolls_made:
            await self._broadcast(self._log_envelope("dice", self._dice_log_text(character.name, roll)))
            await self._broadcast(self._dice_result_envelope(player_id, roll))

        # A direct owner ask: damage/heal/spell/item/condition should read
        # differently at a glance in the log, not blend into plain
        # narration text - broadcast outright (not owner-only) since HP/
        # conditions changing is already narratively public the same way
        # player_update/npc_update already are.
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
        for npc_name, xp_award, party_results in xp_awards:
            text = _party_xp_announcement(npc_name, xp_award, party_results)
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

        if missed_change_corrected:
            # A real correction actually landed above (via check_missed_change),
            # not just a passive flag - lets the player know the sheet was
            # double-checked and fixed, rather than either staying silent
            # or still showing the "might be out of sync" warning below,
            # which the sheet_changed/npcs_touched state a real correction
            # just set would suppress anyway (see that condition below).
            # Deliberately not advisory=True - the client renders that flag
            # with a yellow warning triangle (client/app.py), the right
            # treatment for "you might want to double check" but wrong for
            # a real confirmation that the sheet's already been fixed.
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

    async def _maybe_update_campaign_summary(self) -> None:
        """Best-effort rolling campaign summary (docs/REBUILD_PLAN.md): every
        CAMPAIGN_SUMMARY_INTERVAL resolved turns, hand the backend the current
        summary plus the history window and let it compress. Failure never
        blocks the turn - the same report-don't-block convention _save()
        already established; a stale/absent summary degrades to exactly the
        pre-summarizer behavior."""
        session = self._session
        if not session.history:
            return
        session.turns_since_summary += 1
        if session.turns_since_summary < CAMPAIGN_SUMMARY_INTERVAL:
            return
        session.turns_since_summary = 0
        summarize = getattr(self._dm, "summarize", None)
        if summarize is None:
            return
        try:
            summary = await summarize(session.campaign_summary, session.history)
            if summary:
                session.campaign_summary = summary
        except Exception:
            logger.exception("Campaign summary update failed for session %s", session.session_id)

    async def _on_chat_message(self, envelope: Envelope) -> None:
        await self._broadcast(self._log_envelope("chat", envelope.payload.get("text", "")))

    async def _on_character_edit(self, envelope: Envelope) -> None:
        """Player-side bookkeeping - notes, adding/removing/equipping an
        inventory item by name - that doesn't need DM adjudication
        (docs/protocol.md). Deliberately the mirror image of apply_update's
        mechanical fields: this handler only ever touches notes/inventory/
        equipped_weapon/equipped_armor (and, as a side effect of the
        latter, ac - see CHARACTER_EDIT_FIELDS above), never hp/conditions/
        stats/xp, so a player editing their own sheet can't grant
        themselves healing or gear out of nowhere the DM never narrated.
        Exempt from turn order like chat_message/dice_roll - only
        _on_player_action checks current_turn."""
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
                    f"Can't edit '{field}' - try notes, add_item, remove_item, equip, or unequip.", level="warning"
                ),
            )
            return

        ac_changed = False

        if field == "notes":
            character.notes = str(value)
        elif field == "add_item":
            # No magic_bonus here - that's the DM tool's own optional field
            # (apply_update, server/state.py), never player-settable, the
            # same "engine/DM decides mechanical state" boundary this
            # handler's own docstring already draws.
            character.add_item(str(value))
        elif field == "remove_item":
            item = str(value)
            if not character.remove_item(item):
                await self._send_to(
                    player_id, self._system_envelope(f"You don't have '{item}' to remove.", level="warning")
                )
                return
            # Removing an equipped item unequips it too - a dangling
            # equipped_weapon/equipped_armor/equipped_shield pointing at
            # something no longer owned would be a real, confusing
            # inconsistency. Stack-aware now: only when no more of that
            # name are left (character.find_item returns None) - removing
            # one potion from a stack of three shouldn't unequip anything,
            # but removing your only equipped weapon should.
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
            entry = self._rules.get_entry("equipment", item)
            if entry is not None and entry.get("damage"):
                character.equipped_weapon = item
            elif entry is not None and entry.get("ac"):
                character.equipped_armor = item
                ac_changed = True
            elif entry is not None and entry.get("ac_bonus"):
                character.equipped_shield = item
                ac_changed = True
            else:
                await self._send_to(
                    player_id,
                    self._system_envelope(f"'{item}' isn't a recognized weapon, armor, or shield.", level="warning"),
                )
                return
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

        # notes/inventory/equipped_weapon/equipped_armor stay private, the
        # same boundary _public_character_view draws - but ac is public
        # (visible combat capability, same as hp), so an equip/unequip
        # that actually changed it also needs the public player_update
        # broadcast every other ac-changing path already sends, not just
        # the private character_update every character_edit sends.
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        if ac_changed:
            await self._broadcast(self._player_update_envelope(character))
        await self._save(player_id)

    async def _on_apply_proposed_change(self, envelope: Envelope) -> None:
        """Applies a server-authored correction proposal the player accepted
        via /apply (docs/protocol.md's "Missed-change confirmable
        proposal"). The proposal was generated by the DM backend when the
        missed-change heuristic fired and check_missed_change declined to
        auto-correct; it only ever carries target/hp_delta/add_condition
        (the MISSED_CHANGE_SCHEMA field set). Exempt from turn order like
        _on_character_edit - this is a correction of what already happened,
        not a narrative action. Applies through the same
        CharacterSheet.apply_update/NPC machinery a real in-turn tool call
        uses, so its broadcasts match a real update_character. The pending
        proposal expires on the player's next action (_on_player_action
        pops it), so a stale suggestion can never be applied late."""
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
            npc_key = target.casefold()
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
        # _dice_roll_tags (module-level, above) builds the shared part of
        # this label - previously duplicated here independently of
        # request_roll's own copy, which had already drifted out of sync
        # (see that function's own docstring). reason/purpose and the
        # critical-hit callout stay specific to this function, the same
        # deliberate difference _dice_roll_tags' docstring explains.
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
        # A save has no skill/spell field of its own to carry proficient/
        # proficiency_bonus alongside (CLASS_SAVING_THROW_PROFICIENCIES) -
        # without this, a proficient save's own bonus was silently missing
        # from the broadcast payload entirely, even though it was already
        # correctly included in the roll's own total.
        if roll.get("roll_kind") == "save" and not roll.get("skill") and not roll.get("spell"):
            payload["proficient"] = roll["proficient"]
            if roll["proficient"]:
                payload["proficiency_bonus"] = roll["proficiency_bonus"]
        if roll.get("disadvantage"):
            payload["disadvantage"] = True
            payload["disadvantage_reasons"] = roll["disadvantage_reasons"]
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
                # The recipient's own entry is a full sheet (inventory
                # included); every other player's entry is the same public
                # view player_joined/player_update broadcast - a (re)joining
                # player shouldn't see everyone else's inventory just
                # because it's bundled into their own sync.
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
                # ROADMAP.md's own long-open "client-visible in-combat
                # indicator" gap - Session.in_combat/turn_order already
                # existed for the mechanical turn cycling, just never
                # reached a (re)joining client to render a persistent
                # combat/initiative-order display.
                "in_combat": self._session.in_combat,
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
            payload={"player_id": player_id, "sheet_delta": _owner_character_view(character, self._rules)},
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
        # in_combat/turn_order ride along here too, not just state_sync -
        # turn_prompt is what already-connected clients receive live on
        # every start_combat/end_combat (and every ordinary turn advance),
        # so this is what keeps an in-combat indicator/initiative-order
        # display current without waiting for a fresh (re)join.
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
        # category is deliberately its own field, not folded into kind -
        # kind ("outcome") says *how* the client should route this line
        # (append to the log), category ("damage"/"heal"/"spell"/
        # "condition"/"item") says *what color* - two independent axes,
        # the same way dice_result already keeps roll_kind and damage_type
        # as separate fields rather than combining them into one enum.
        if category is not None:
            payload["category"] = category
        return Envelope(type="log_entry", session_id=self._session.session_id, sender_id="server", payload=payload)

    def _system_envelope(self, text: str, level: str = "info", advisory: bool = False, proposed_change: dict | None = None) -> Envelope:
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
        if proposed_change is not None:
            payload["proposed_change"] = proposed_change
        return Envelope(
            type="system_message",
            session_id=self._session.session_id,
            sender_id="server",
            payload=payload,
        )
