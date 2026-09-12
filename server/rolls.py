"""Dice, AC, and roll-shaping helpers, split out of engine.py. Pure functions -
no engine state, no I/O. `server.engine` re-exports the names tests reach for."""

from __future__ import annotations

import re

from . import dice
from .rules import RulesIndex, slug
from .state import CharacterSheet

# Consumables with a real, coded effect when "used" - keyed by the same
# slug() key equip/unequip already resolve item names through. Anything
# SRD tags as a Potion but isn't listed here (e.g. Potion of Climbing,
# whose real effect is a temporary climb-speed buff - no duration/buff
# system exists to apply it) has no coded effect yet: a real, honest gap,
# not a silently-wrong "nothing happens" catch-all - see _use_item and
# server/views.py's _item_view (usable=False hides the button entirely
# rather than showing one that does nothing).
CONSUMABLE_EFFECTS: dict[str, str] = {
    "potion_of_healing": "2d4+2",  # SRD's own worded effect, verbatim.
}

# NPC introduced without a real max_hp from lookup_rule - a CR-1/4 mook's
# HP, a trivial fight rather than a 100-HP sponge.
DEFAULT_NPC_HP = 10

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


def is_weapon_proficient(character: CharacterSheet, weapon_name: str, rules: RulesIndex) -> bool:
    """Real 5e weapon proficiency: a class-level category ("simple"/
    "martial", matched against the weapon's own SRD category - e.g.
    "Martial Melee Weapon") or a specific named weapon a class lists
    individually (e.g. a wizard's "light crossbows" - de-pluralized via a
    plain rstrip("s") and matched against the weapon's real SRD name;
    every entry in srd.json's classes[*].proficiencies.weapons today is a
    regular plural, so this needs no exception list). An unrecognized
    weapon or class means "can't confirm proficiency" -> not proficient,
    the same "unrecognized = no bonus assumed" rule request_roll's own
    weapon field already follows elsewhere - never silently grants it."""
    entry = rules.get_entry("equipment", weapon_name)
    class_entry = rules.get_entry("class", character.character_class)
    if entry is None or class_entry is None:
        return False
    category = entry.get("category", "").lower()
    real_name = entry.get("name", "").lower()
    for prof in class_entry.get("proficiencies", {}).get("weapons", []):
        prof = prof.lower()
        if prof == "simple" and "simple" in category:
            return True
        if prof == "martial" and "martial" in category:
            return True
        if prof.rstrip("s") == real_name:
            return True
    return False


def _equip_slot_for(item_name: str, rules: RulesIndex) -> str | None:
    """Which equip slot `item_name` fills - weapon/armor/shield - from real
    SRD data, or None if it isn't equippable at all (a torch, a potion,
    ...). Shared by server/engine.py's equip handler and
    server/views.py's _item_view (so the sheet UI can hide 'equip' on
    something the handler would reject anyway) - previously duplicated
    inline in the handler alone."""
    entry = rules.get_entry("equipment", item_name)
    if entry is None:
        return None
    if entry.get("damage"):
        return "weapon"
    if entry.get("ac"):
        return "armor"
    if entry.get("ac_bonus"):
        return "shield"
    return None


def _use_item(character: CharacterSheet, item_name: str) -> tuple[str, bool]:
    """Applies a player-initiated character_edit `use_item` - the one
    exception to "character_edit never touches hp" (see engine.py's
    _on_character_edit docstring): it only ever consumes something
    already legitimately in the character's own inventory, granted
    there by the DM in the first place, never fabricates a new resource.
    Returns (message, changed), matching _cast_spell's shape above."""
    if character.find_item(item_name) is None:
        return f"you don't have '{item_name}' to use.", False
    heal_dice = CONSUMABLE_EFFECTS.get(slug(item_name))
    if heal_dice is None:
        return f"'{item_name}' has no usable effect.", False
    total, _, _ = dice.roll(heal_dice)
    character.apply_update({"hp_delta": total})
    character.remove_item(item_name)
    return (
        f"drinks a {item_name} ({heal_dice} → {total}), now at {character.hp}/{character.max_hp} HP.",
        True,
    )


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
