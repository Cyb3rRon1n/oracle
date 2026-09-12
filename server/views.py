"""Payload-shaping helpers - the public/owner character views, the attack
table, the NPC roster line - split out of engine.py. `server.engine`
re-exports the names tests reach for."""

from __future__ import annotations

from .character_build import CLASS_SAVING_THROW_PROFICIENCIES, CLASS_SKILL_PROFICIENCIES
from .rolls import CONSUMABLE_EFFECTS, _equip_slot_for
from .rules import RulesIndex, slug
from .state import SPELLCASTING_ABILITY, CharacterSheet, InventoryItem, Session

NPC_NOTES_CONTEXT_MAX_CHARS = 80


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
        "portrait": character.portrait,
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


def _item_view(item: InventoryItem, rules: RulesIndex) -> dict:
    """One inventory item as sent to its owner: the stored fields plus two
    server-computed capability flags, so the sheet UI never has to
    duplicate SRD-category knowledge to decide what a button should do -
    'equip' only for something a real weapon/armor/shield entry resolves
    to (_equip_slot_for), 'use' only for a real coded consumable effect
    (server/rolls.py's CONSUMABLE_EFFECTS)."""
    return {
        **item.model_dump(),
        "equippable": _equip_slot_for(item.name, rules) is not None,
        "usable": slug(item.name) in CONSUMABLE_EFFECTS,
    }


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
        "inventory": [_item_view(item, rules) for item in character.inventory],
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
        # Display-only SRD data (srd.json): armor/weapon/tool proficiency from
        # the class, spoken languages from the race. Nothing gates on them yet.
        "class_proficiencies": (class_entry or {}).get("proficiencies", {}),
        "languages": list((race_entry or {}).get("languages", [])),
    }


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
