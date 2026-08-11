from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "srd.json"

CATEGORY_KEYS = {
    "monster": "monsters",
    "spell": "spells",
    "class": "classes",
    "equipment": "equipment",
    "condition": "conditions",
}


class RulesIndex:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load_default(cls) -> "RulesIndex":
        return cls(json.loads(_DATA_PATH.read_text()))

    def lookup(self, category: str, name: str) -> str:
        key = CATEGORY_KEYS.get(category)
        if key is None:
            return f"Unknown category '{category}'. Valid categories: {', '.join(CATEGORY_KEYS)}."

        entry = self.get_entry(category, name)
        if entry is None:
            return f"No local SRD entry found for {category} '{name}'."
        return json.dumps(entry, indent=2)

    def get_entry(self, category: str, name: str) -> dict | None:
        """Structured lookup for callers that need real field access (e.g.
        character creation reading a class's hit_die), as opposed to
        lookup()'s JSON-string-for-narration shape."""
        key = CATEGORY_KEYS.get(category)
        if key is None:
            return None
        entries = self._data.get(key, {})
        return entries.get(slug(name))

    def all_entries(self, category: str) -> dict[str, dict]:
        """Every entry in a category, keyed by its internal slug - for
        callers that need to enumerate a whole table (a data-integrity
        check, a future "list known X" feature), not look up one known
        name. {} for an unrecognized category, the same graceful-miss
        convention get_entry already follows."""
        key = CATEGORY_KEYS.get(category)
        if key is None:
            return {}
        return dict(self._data.get(key, {}))

    def xp_thresholds(self) -> dict[int, int]:
        """Level -> cumulative XP required to reach it, from the SRD's own
        Character Advancement table. Not name-keyed like CATEGORY_KEYS'
        entries, so this gets its own accessor rather than going through
        get_entry() - server/state.py's CharacterSheet.gain_xp() takes this
        directly."""
        return {int(level): xp for level, xp in self._data["leveling"]["xp_by_level"].items()}

    def spell_slots_by_level(self, level: int) -> dict[str, int]:
        """Real 5e's full-caster spell slot table (slot level -> count) for
        a given character level, from the SRD's own Spell Slots by Level
        table. {} for a level with no entry (shouldn't happen for 1-20, but
        graceful rather than a KeyError for a level outside that range) -
        the same "not present isn't an error" convention xp_for_cr already
        follows."""
        return dict(self._data["leveling"]["spell_slots_by_level"].get(str(level), {}))

    def xp_for_cr(self, challenge_rating: str) -> int | None:
        """XP awarded for defeating a monster of the given challenge rating
        (e.g. "1/4", "2"), from the SRD's own Experience Points by
        Challenge Rating table. None if the CR string isn't recognized -
        callers fall back to a flat default rather than treating this as
        an error, the same "not present isn't an error" convention
        detect_gpu()-style functions across this workspace already follow."""
        return self._data["leveling"]["xp_by_cr"].get(challenge_rating)


def slug(name: str) -> str:
    """Normalizes a display name (or an already-slugged one) to srd.json's
    own key format - e.g. "Fire Bolt" and "fire_bolt" both become
    "fire_bolt". Public (not the get_entry()-only private helper this used
    to be) since server/engine.py's cast_spell handling needs the exact
    same normalization to check a spell name against a character's
    known_spells (itself stored pre-slugged, e.g. CLASS_KNOWN_SPELLS) -
    one place defines this instead of two independent implementations
    drifting apart."""
    return name.strip().lower().replace(" ", "_").replace("'", "")
