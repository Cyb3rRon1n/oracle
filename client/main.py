from __future__ import annotations

import uuid
from pathlib import Path

from .app import DungeonMasterApp

PLAYER_ID_FILE = Path(".player_id")


def _get_or_create_player_id() -> str:
    if PLAYER_ID_FILE.exists():
        return PLAYER_ID_FILE.read_text().strip()
    player_id = str(uuid.uuid4())
    PLAYER_ID_FILE.write_text(player_id)
    return player_id


def main() -> None:
    player_id = _get_or_create_player_id()
    player_name = input("Character name: ") or "Adventurer"
    session_id = input("Session ID (blank for default): ") or "default"

    app = DungeonMasterApp(
        uri="ws://localhost:8765",
        session_id=session_id,
        player_id=player_id,
        player_name=player_name,
    )
    app.run()


if __name__ == "__main__":
    main()
