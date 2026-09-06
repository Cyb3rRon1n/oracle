"""Character creation and the per-class / per-race SRD data behind it, split
out of engine.py. `server.engine` re-exports the names tests reach for."""

from __future__ import annotations

from pydantic import ValidationError

from .lore import OriginTable, load_default_origin_table, random_origin
from .rolls import _compute_ac
from .rules import RulesIndex
from .state import ABILITY_KEYS, CharacterSheet, InventoryItem, ability_modifier

# Added once to level-1 HP (on top of hit-die max + CON), level-1 only -
# combat stays lethal-if-careless without being one-crit-fatal at low HP.
STARTING_HP_CUSHION = 10

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


def _hit_die_max(hit_die: str) -> int:
    # "d10" -> 10. ponytail: max roll, not a per-level roll. Callers add CON.
    return int(hit_die.lstrip("d"))


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
