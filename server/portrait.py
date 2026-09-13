"""Builds a portrait prompt from a character's own real sheet data - same
pattern as nightwire's _ROLE_VISUALS/_LIFEPATH_VISUALS, adapted to oracle's
domain. Visual (appearance) descriptors, not mechanical ones - server/rules/
srd.json's classes/races describe game mechanics, not looks, so this is
genuinely new content, not a duplicate of something already in the codebase."""

from __future__ import annotations

from .state import CharacterSheet, WorldState

_CLASS_VISUALS: dict[str, str] = {
    "fighter": "in battle-worn plate or chainmail, a sword at their hip, a soldier's bearing",
    "wizard": "in a long traveling robe, a spellbook or component pouch at their belt, a scholar's air",
    "rogue": "in dark leathers built for moving unseen, daggers concealed, a sly half-smile",
    "cleric": "in vestments over armor, a holy symbol worn openly, a calm and steady presence",
}

# Short descriptive tags, not full sentences - composed alongside
# _CLASS_VISUALS and background below, not standalone prose. No entry for
# "human" - real 5e's own framing (srd.json's racial_traits: "no single
# defining trait, a well-rounded aptitude") means there's nothing
# distinctive to add here, the same reason nightwire's own visual tables
# have no "standard" entry either.
_RACE_VISUALS: dict[str, str] = {
    "elf": "tall and angular, pointed ears",
    "high_elf": "sharp-featured, pointed ears, an aristocratic bearing",
    "wood_elf": "lean and weathered, pointed ears, dressed for the wilds",
    "dwarf": "stout and broad-shouldered, a braided beard",
    "hill_dwarf": "stocky and weathered, a braided beard",
    "mountain_dwarf": "powerfully built, a braided beard, heavy boots",
    "halfling": "small and nimble, a cheerful face",
    "lightfoot_halfling": "small and quick-footed, a cheerful face",
    "stout_halfling": "small and sturdy, a cheerful weathered face",
}

# Player-chosen art direction, applied as a fixed suffix tag - not a LoRA
# or checkpoint swap (a bare ComfyUI install has no style LoRAs to assume),
# so every option works with just a base checkpoint. Keep in sync with
# web/src/components/Avatar.jsx's own STYLE_OPTIONS list (small and fixed
# enough that mirroring it by hand beats a server-announces-styles
# mechanism for four entries).
DEFAULT_STYLE = "fantasy"
STYLE_PRESETS: dict[str, str] = {
    "fantasy": "fantasy character portrait, dramatic lighting, highly detailed digital painting",
    "anime": "anime character portrait, cel-shaded, vibrant colors, clean line art",
    "comic": "comic book character portrait, bold ink lines, halftone shading, dynamic pose",
    "realistic": "photorealistic character portrait, natural lighting, high detail, 85mm lens",
}


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def build_portrait_prompt(character: CharacterSheet, style: str = DEFAULT_STYLE) -> str:
    race = character.race or "human"
    character_class = character.character_class or "adventurer"
    base = f"{character.name}, {_article(race)} {race} {character_class}"
    style_suffix = STYLE_PRESETS.get(style, STYLE_PRESETS[DEFAULT_STYLE])

    tags = ", ".join(
        tag
        for tag in (_RACE_VISUALS.get(race.lower()), _CLASS_VISUALS.get(character_class.lower()), character.background)
        if tag
    )
    if tags:
        return f"{base} — {tags}, {style_suffix}"
    return f"{base} — {style_suffix}"


# Environment-art wording for the same 4 style keys build_portrait_prompt's
# STYLE_PRESETS uses - "fantasy character portrait" wouldn't fit a wide
# establishing shot of a tavern, so this is a sibling table, not a reuse.
# Keep in sync with STYLE_OPTIONS in web/src/components/SceneBanner.jsx.
SCENE_STYLE_PRESETS: dict[str, str] = {
    "fantasy": "fantasy environment concept art, dramatic lighting, highly detailed digital painting",
    "anime": "anime background art, cel-shaded, vibrant colors, clean line art",
    "comic": "comic book splash page background, bold ink lines, halftone shading",
    "realistic": "photorealistic establishing shot, natural lighting, high detail",
}


def build_scene_prompt(world: WorldState, style: str = DEFAULT_STYLE) -> str:
    location = world.location if world.location and world.location != "unknown" else "a fantasy adventure setting"
    detail = f"{location} — {world.mood}" if world.mood else location
    style_suffix = SCENE_STYLE_PRESETS.get(style, SCENE_STYLE_PRESETS[DEFAULT_STYLE])
    return f"A wide establishing shot of {detail}, {style_suffix}"
