from __future__ import annotations

import uuid

from .app import DungeonMasterApp


def main() -> None:
    player_id = str(uuid.uuid4())
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
