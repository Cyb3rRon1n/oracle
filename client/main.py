from __future__ import annotations

import os

from dotenv import load_dotenv

from .app import DungeonMasterApp


def main() -> None:
    # Name/session ID/class/login are all gathered inside the running
    # Textual app now (LoginScreen, then WelcomeScreen, client/app.py) -
    # nothing blocking runs here before app.run(). Player identity used
    # to be a local .player_id file generated once per machine; real
    # server-owned identity (ROADMAP.md, 2026-08-13) replaced that with a
    # real login the server itself validates, so there's no local
    # identity file to read/create here anymore - player_id is unknown
    # until LoginScreen's own real login_result comes back over the wire.
    load_dotenv()
    uri = os.environ.get("SERVER_URI", "ws://localhost:8765")
    app = DungeonMasterApp(uri=uri)
    app.run()


if __name__ == "__main__":
    main()
