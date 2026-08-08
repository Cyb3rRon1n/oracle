from __future__ import annotations

import uuid
from pathlib import Path

from .app import DungeonMasterApp

PLAYER_ID_FILE = Path(".player_id")


def _get_or_create_player_id() -> tuple[str, bool]:
    """Returns (player_id, is_new) - is_new is False for a returning
    player, so WelcomeScreen knows whether to ask for a class at all (an
    existing character keeps whatever it already has; reconnecting
    shouldn't re-ask)."""
    if PLAYER_ID_FILE.exists():
        return PLAYER_ID_FILE.read_text().strip(), False
    player_id = str(uuid.uuid4())
    PLAYER_ID_FILE.write_text(player_id)
    return player_id, True


def main() -> None:
    # Name/session ID/class used to be gathered here via blocking input()
    # calls before the Textual app even launched - those can't run inside
    # a live Textual event loop, so they're now WelcomeScreen's job
    # (client/app.py), the client's actual pre-game main menu entry point.
    # Local player identity (this file's whole remaining job) stays outside
    # the TUI - it's filesystem state, not something to gather via widgets.
    player_id, is_new = _get_or_create_player_id()
    app = DungeonMasterApp(uri="ws://localhost:8765", player_id=player_id, is_new_character=is_new)
    app.run()


if __name__ == "__main__":
    main()
