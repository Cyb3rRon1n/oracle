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

        entries = self._data.get(key, {})
        entry = entries.get(_slug(name))
        if entry is None:
            return f"No local SRD entry found for {category} '{name}'."
        return json.dumps(entry, indent=2)


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("'", "")
