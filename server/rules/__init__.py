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
        return entries.get(_slug(name))

    def xp_thresholds(self) -> dict[int, int]:
        """Level -> cumulative XP required to reach it, from the SRD's own
        Character Advancement table. Not name-keyed like CATEGORY_KEYS'
        entries, so this gets its own accessor rather than going through
        get_entry() - server/state.py's CharacterSheet.gain_xp() takes this
        directly."""
        return {int(level): xp for level, xp in self._data["leveling"]["xp_by_level"].items()}

    def xp_for_cr(self, challenge_rating: str) -> int | None:
        """XP awarded for defeating a monster of the given challenge rating
        (e.g. "1/4", "2"), from the SRD's own Experience Points by
        Challenge Rating table. None if the CR string isn't recognized -
        callers fall back to a flat default rather than treating this as
        an error, the same "not present isn't an error" convention
        detect_gpu()-style functions across this workspace already follow."""
        return self._data["leveling"]["xp_by_cr"].get(challenge_rating)


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("'", "")
