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

# NPC introduced without a real max_hp from lookup_rule - a CR-1/4 mook's
# HP, a trivial fight rather than a 100-HP sponge.
DEFAULT_NPC_HP = 10

# Added once to level-1 HP (on top of hit-die max + CON), level-1 only -
# combat stays lethal-if-careless without being one-crit-fatal at low HP.
STARTING_HP_CUSHION = 10

# Resolved turns between campaign-summary rebuilds (window holds ~6).
CAMPAIGN_SUMMARY_INTERVAL = 10

# Fact-ledger injection: newest facts always reach the DM; older ones only
# when one of their 5+ char words appears in the current action/location.
LEDGER_RECENT_LIMIT = 12
LEDGER_RELEVANT_LIMIT = 8

# XP for an NPC with no known-monster CR and no explicit "xp" override
# (CR 1/4's SRD value - see _xp_for_npc).
DEFAULT_NPC_XP = 50

# Conditions that impose disadvantage on the bearer's OWN rolls. Subset of
# the five tracked: grappled (movement-only, no movement system) and
# stunned (target-side/turn-blocking, not modeled) are deliberately out.
DISADVANTAGE_CONDITIONS = frozenset({"poisoned", "frightened", "prone"})

# Per-condition roll_kind narrowing, per SRD text: poisoned/frightened hit
# attacks + checks but not saves; prone hits only attacks. Only applies
# when roll_kind is given (request_roll, Anthropic); an omitted roll_kind
# keeps the broader "any roll" behavior.
ROLL_KIND_DISADVANTAGE_EXCLUSIONS: dict[str, frozenset[str]] = {
    "poisoned": frozenset({"save"}),
    "frightened": frozenset({"save"}),
    "prone": frozenset({"save", "check"}),
}

# What a player may set on their own sheet via character_edit: pure
# fiction/bookkeeping only. hp/conditions/stats/xp stay DM- or engine-only.
# equip/unequip change AC as a side effect but the player only names an
# owned item, never a number (_compute_ac does the rest). The DM reads the
# RP text fields via character_summary but never writes them.
CHARACTER_EDIT_TEXT_FIELDS = frozenset({"notes", "personality", "ideals", "bonds", "flaws", "alignment"})
CHARACTER_EDIT_FIELDS = CHARACTER_EDIT_TEXT_FIELDS | frozenset(
    {"add_item", "remove_item", "equip", "unequip"}
)


def _has_disadvantage(character: CharacterSheet, roll_kind: str | None = None) -> list[str]:
    """Which of the character's conditions trigger disadvantage (empty if
    none). A list, not a bool, so callers can name the reason in the roll
    text; disadvantage never stacks, so `bool(...)` is all that matters
    mechanically. roll_kind ("attack"/"save"/"check") narrows the result
    via ROLL_KIND_DISADVANTAGE_EXCLUSIONS when given."""
    reasons = []
    for c in character.conditions:
        key = c.casefold()
        if key not in DISADVANTAGE_CONDITIONS:
            continue
        if roll_kind is not None and roll_kind in ROLL_KIND_DISADVANTAGE_EXCLUSIONS.get(key, frozenset()):
            continue
        reasons.append(c)
    return reasons


# Per-class starting kit. A fixed subset, not a full 5e chargen (no
# player-chosen equipment/stats yet).
CLASS_STARTING_EQUIPMENT: dict[str, list[str]] = {
    "fighter": ["Longsword", "Leather Armor"],
    "rogue": ["Shortbow", "Leather Armor"],
    "cleric": ["Leather Armor", "Potion of Healing"],
    "wizard": ["Potion of Healing"],
}

# Fixed per-class known spells, assigned once at creation (no daily
# preparation modeled). A spell above the character's current slot level
# is still "known", just not castable - the cast-time slot check is the
# only gate. Fighter/rogue: no entry, cast nothing.
CLASS_KNOWN_SPELLS: dict[str, list[str]] = {
    "wizard": [
        "fire_bolt", "ray_of_frost", "magic_missile", "mage_armor", "shield", "fireball",
        "burning_hands", "misty_step", "sleep", "charm_person", "thunderwave",
        "hold_person", "web", "fly",
    ],
    "cleric": [
        "sacred_flame", "guidance", "cure_wounds", "bless", "healing_word", "spiritual_weapon",
        "inflict_wounds", "shield_of_faith", "guiding_bolt", "hold_person", "lesser_restoration",
        "revivify",
    ],
}

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

# The SRD Standard Array.
STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

# Order in which the Standard Array is assigned to abilities, per class.
# Hand-written: primary stat first, CON second (survival), the rest by
# archetype. NOT the same as real saving-throw proficiency - that has its
# own table (CLASS_SAVING_THROW_PROFICIENCIES). Blank class: no entry, no
# stats.
CLASS_ABILITY_PRIORITY: dict[str, tuple[str, ...]] = {
    "fighter": ("str", "con", "dex", "wis", "cha", "int"),
    "wizard": ("int", "con", "dex", "wis", "cha", "str"),
    "rogue": ("dex", "con", "int", "wis", "cha", "str"),
    "cleric": ("wis", "con", "str", "dex", "cha", "int"),
}

# Fixed per-class skill proficiencies (no player choice modeled). Blank
# class: proficient in nothing.
CLASS_SKILL_PROFICIENCIES: dict[str, tuple[str, ...]] = {
    "fighter": ("athletics", "perception"),
    "wizard": ("arcana", "investigation"),
    "rogue": ("stealth", "sleight_of_hand", "perception", "deception"),
    "cleric": ("insight", "religion"),
}

# The SRD's two proficient saving-throw abilities per class. Proficiency
# bonus applies to a save only when its ability is one of these. Blank
# class: no proficient saves.
CLASS_SAVING_THROW_PROFICIENCIES: dict[str, tuple[str, str]] = {
    "fighter": ("str", "con"),
    "wizard": ("int", "wis"),
    "rogue": ("dex", "int"),
    "cleric": ("wis", "cha"),
}

# SRD baseline ASI levels (no subclass extras).
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
        # Public like name/class, not private like inventory/stats/notes.
        "race": character.race,
        "hp": character.hp,
        "max_hp": character.max_hp,
        "ac": character.ac,
        "conditions": list(character.conditions),
        # dying/dead are urgent public facts; raw death_save counts stay
        # owner-only (full model_dump() below).
        "dying": character.dying,
        "dead": character.dead,
        # level is public, xp stays owner-only.
        "level": character.level,
    }


def _class_features_for(class_entry: dict | None, level: int) -> list[str]:
    """Every class feature earned through `level`: level_1_features plus each
    features_by_level entry, accumulated. Derived from (class, level) on every
    view build, not stored. ASI-only / subclass-only levels have no srd.json
    entry (ASI math is applied separately; subclasses are out of scope)."""
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


def _sign(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def _attack_lines(character: CharacterSheet, rules: RulesIndex, spell_attack_bonus: int | None) -> list[dict]:
    """The paper sheet's Attacks & Spellcasting table, resolved server-side
    so the client never guesses a number: the equipped weapon plus every
    attack-shaped known spell, each as {name, kind, to_hit, damage}.

    Weapon ability follows real 5e - DEX for a ranged weapon, the better of
    STR/DEX for a finesse weapon, otherwise STR. Proficiency is assumed
    (Oracle tracks no weapon proficiencies - see docs/character-sheet-gaps.md);
    a real magic_bonus on the carried weapon adds to both rolls. Spell
    to-hit is the caster's own spell_attack_bonus; spell damage is the
    SRD die as-is (no ability mod - real 5e's rule for spell damage).
    Empty for a bare-handed non-caster."""
    lines: list[dict] = []
    prof = character.proficiency_bonus
    mods = character.stat_modifiers

    weapon = character.equipped_weapon
    entry = rules.get_entry("equipment", weapon) if weapon else None
    if entry and entry.get("damage"):
        die, _, dtype = entry["damage"].partition(" ")
        category = entry.get("category", "").lower()
        props = entry.get("properties", "").lower()
        if "ranged" in category:
            ability = "dex"
        elif "finesse" in props:
            ability = "dex" if mods.get("dex", 0) >= mods.get("str", 0) else "str"
        else:
            ability = "str"
        magic = getattr(character.find_item(weapon), "magic_bonus", 0) or 0
        ability_mod = mods.get(ability, 0)
        to_hit = prof + ability_mod + magic
        dmg_mod = ability_mod + magic
        damage = f"{die}{_sign(dmg_mod)} {dtype}" if dmg_mod else f"{die} {dtype}"
        lines.append({"name": entry["name"], "kind": "weapon", "to_hit": _sign(to_hit), "damage": damage})

    if spell_attack_bonus is not None:
        for slug_name in character.known_spells:
            spell = rules.get_entry("spell", slug_name)
            if spell and spell.get("attack") and spell.get("damage"):
                die, _, dtype = spell["damage"].partition(" ")
                lines.append({
                    "name": spell["name"], "kind": "spell",
                    "to_hit": _sign(spell_attack_bonus), "damage": f"{die} {dtype}",
                })
    return lines


def _owner_character_view(character: CharacterSheet, rules: RulesIndex) -> dict:
    """The owner's own full sheet: model_dump() plus derived fields the sheet
    UI needs - class features, skill/save proficiencies, spell attack bonus,
    resolved attacks, and the XP thresholds bracketing the current level.
    Backs both the state_sync and character_update owner payloads."""
    class_entry = rules.get_entry("class", character.character_class)
    race_entry = rules.get_entry("race", character.race) if character.race else None
    class_key = character.character_class.strip().lower()
    # Spell attack bonus - real 5e: proficiency + spellcasting ability
    # modifier. The save DC (8 + those two) is already a computed field on
    # the sheet; this is the attack-roll counterpart, sent the same way so
    # the character sheet UI can show both. None for a non-caster.
    spell_ability = SPELLCASTING_ABILITY.get(class_key)
    spell_attack_bonus = (
        character.proficiency_bonus + character.stat_modifiers[spell_ability]
        if spell_ability and spell_ability in character.stat_modifiers
        else None
    )
    # XP bar bounds: cumulative XP to reach the current level and the next.
    # xp_next is None at max level (no entry past 20).
    thresholds = rules.xp_thresholds()
    return {
        **character.model_dump(),
        "class_features": _class_features_for(class_entry, character.level),
        "racial_traits": list((race_entry or {}).get("traits", [])),
        "xp_level_start": thresholds.get(character.level, 0),
        "xp_next_level": thresholds.get(character.level + 1),
        "skill_proficiencies": list(CLASS_SKILL_PROFICIENCIES.get(class_key, ())),
        # Which two saving throws this class is proficient in - already used
        # for real save rolls (CLASS_SAVING_THROW_PROFICIENCIES), now also
        # surfaced so the sheet can mark them, the same as skill_proficiencies.
        "saving_throw_proficiencies": list(CLASS_SAVING_THROW_PROFICIENCIES.get(class_key, ())),
        "spell_attack_bonus": spell_attack_bonus,
        "attacks": _attack_lines(character, rules, spell_attack_bonus),
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
    inspiration_label = " (advantage: Inspiration)" if roll.get("inspiration") else ""
    return (
        damage_label + weapon_magic_label + ability_label + skill_label + spell_label
        + roll_kind_label + disadvantage_label + inspiration_label
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
    if update.get("add_item") or update.get("remove_item") or update.get("gold_delta"):
        return "item"
    return None


def _hit_die_max(hit_die: str) -> int:
    # "d10" -> 10. ponytail: max roll, not a per-level roll. Callers add CON.
    return int(hit_die.lstrip("d"))


def _xp_for_npc(npc: CharacterSheet, update: dict, rules: RulesIndex) -> int:
    """XP for defeating this NPC, in priority order: (1) explicit "xp" in the
    killing update; (2) the NPC's name matched to an SRD monster, its "cr" run
    through xp_for_cr; (3) DEFAULT_NPC_XP as a safety net."""
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
    or falls back to a blank hp=100/max_hp=100 sheet for a blank or
    unrecognized class - keeps old clients/tests that don't send
    character_class at all working.

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
    origin = random_origin(origin_table or load_default_origin_table())
    background = origin.sheet_summary()
    # The four RP anchors, seeded from the same origin roll - player-
    # editable afterwards via character_edit (CHARACTER_EDIT_FIELDS below).
    # personality reuses the trait the origin already rolled.
    rp_fields = {
        "personality": origin.trait,
        "ideals": origin.ideal,
        "bonds": origin.bond,
        "flaws": origin.flaw,
    }

    race_entry = rules.get_entry("race", race) if race else None
    race_name = race_entry["name"] if race_entry else ""
    speed = (race_entry or {}).get("speed", 30)

    class_entry = rules.get_entry("class", character_class) if character_class else None
    if class_entry is None:
        # No hit die to draw from - a bare baseline (the original classless
        # HP) plus the same cushion every character gets.
        blank_hp = 10 + STARTING_HP_CUSHION
        return CharacterSheet(
            player_id=player_id, name=name, hp=blank_hp, max_hp=blank_hp, background=background,
            race=race_name, speed=speed, **rp_fields,
        )

    stats = _apply_race_bonus(_generate_stats(character_class, stat_priority), race_entry)
    con_mod = ability_modifier(stats["con"]) if stats else 0
    # SRD level-1 HP (hit die max + CON modifier) plus a flat cushion - see
    # STARTING_HP_CUSHION. Floored at 1 so a brutal CON score can't produce
    # a 0- or negative-HP character. Level-up growth is pure SRD (_grant_levels).
    max_hp = max(1, _hit_die_max(class_entry["hit_die"]) + con_mod + STARTING_HP_CUSHION)
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
        hit_die=class_entry["hit_die"],
        race=race_name,
        speed=speed,
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
        **rp_fields,
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
    ):
        self._session = session
        self._dm = dm
        self._broadcast = broadcast
        self._send_to = send_to
        self._store = store
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
        # World-context lorebook (docs/protocol.md): empty until context_select;
        # rebuilt from disk on every selection change and on load.
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

        # Turn-taking is only visible once the adventure has started; a reconnect
        # into a started game should still see whose turn it is.
        if self._has_started():
            # Rescue a queue left pointing at a player who is long gone.
            if await self._skip_absent_players() or self._session.current_turn == player_id:
                await self._broadcast(self._turn_prompt_envelope())

    def _has_started(self) -> bool:
        # bool(log) is the fallback for a session saved before Session.started
        # existed - it would load as False despite real narration history.
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
        if self._seed_world_map():
            # Push the seeded map now - otherwise it only reaches a client on
            # its next full state_sync.
            await self._broadcast(self._world_update_envelope())
        await self._save(envelope.sender_id)

        player_id = envelope.sender_id
        character = self._session.characters.get(player_id) or next(iter(self._session.characters.values()))

        roster = list(self._session.characters.values())
        if len(roster) > 1:
            # A richer prompt so the opening narration acknowledges everyone
            # present; tool-routing still anchors on one character.
            names = ", ".join(
                f"{c.name} the {c.character_class}" if c.character_class else c.name for c in roster
            )
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
            # The 4-action cap is a protocol guarantee (docs/protocol.md),
            # enforced here server-side rather than trusted to each backend.
            trimmed = dict(facts)
            trimmed["suggested_actions"] = list(facts.get("suggested_actions", []))[:4]
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
        """Player-side bookkeeping that doesn't need DM adjudication: the RP
        text fields, and adding/removing/equipping inventory by name (docs/
        protocol.md). Never touches hp/conditions/stats/xp, so a player can't
        grant themselves healing or gear. Exempt from turn order."""
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
                    "Can't edit '{}' - try {}, add_item, remove_item, equip, or unequip.".format(
                        field, ", ".join(sorted(CHARACTER_EDIT_TEXT_FIELDS))
                    ),
                    level="warning",
                ),
            )
            return

        ac_changed = False

        if field in CHARACTER_EDIT_TEXT_FIELDS:
            setattr(character, field, str(value))
        elif field == "add_item":
            # No magic_bonus - that's the DM tool's field, never player-settable.
            character.add_item(str(value))
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

        # The edited fields are private, but ac is public - so an equip that
        # changed it needs the public player_update broadcast too.
        await self._send_to(player_id, self._character_update_envelope(player_id, character))
        if ac_changed:
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
        # Broadcast, public-view-only payload.
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
        # Empty payload - a lifecycle signal to leave the lobby view. Fires
        # unconditionally, not inferred from the first narration (best-effort).
        return Envelope(
            type="session_started",
            session_id=self._session.session_id,
            sender_id="server",
            payload={},
        )

    def _player_update_envelope(self, character: CharacterSheet) -> Envelope:
        # Public counterpart to the private full-sheet _character_update_envelope.
        return Envelope(
            type="player_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload=_public_character_view(character),
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
