from __future__ import annotations

import uuid
from pathlib import Path

from .app import DungeonMasterApp

PLAYER_ID_FILE = Path(".player_id")

# Mirrors server/engine.py's CLASS_STARTING_EQUIPMENT keys - the SRD dataset
# (server/rules/srd.json) is the real source of truth for what a class
# grants; this is just the same short list for the prompt. An unrecognized
# or blank entry falls back gracefully server-side, so this isn't strictly
# validated here.
CHARACTER_CLASSES = ["fighter", "wizard", "rogue", "cleric"]


def _get_or_create_player_id() -> tuple[str, bool]:
    """Returns (player_id, is_new) - is_new is False for a returning
    player, so main() knows whether to prompt for a class at all (an
    existing character keeps whatever it already has; reconnecting
    shouldn't re-ask)."""
    if PLAYER_ID_FILE.exists():
        return PLAYER_ID_FILE.read_text().strip(), False
    player_id = str(uuid.uuid4())
    PLAYER_ID_FILE.write_text(player_id)
    return player_id, True


def main() -> None:
    player_id, is_new = _get_or_create_player_id()
    player_name = input("Character name: ") or "Adventurer"
    session_id = input("Session ID (blank for default): ") or "default"

    character_class = ""
    if is_new:
        character_class = input(f"Class ({'/'.join(CHARACTER_CLASSES)}, blank to skip): ").strip()

    app = DungeonMasterApp(
        uri="ws://localhost:8765",
        session_id=session_id,
        player_id=player_id,
        player_name=player_name,
        character_class=character_class,
    )
    app.run()


if __name__ == "__main__":
    main()
