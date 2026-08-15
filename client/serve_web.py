from __future__ import annotations

import os

from textual_serve.server import Server


def main() -> None:
    # Real browser hosting (ROADMAP.md, 2026-08-13 design discussion) -
    # Textualize's own official tool for exactly this: the existing
    # client.main process keeps running unchanged, textual-serve only
    # streams its drawn frames to a browser over a websocket and
    # streams keystrokes/clicks back. No game-logic changes anywhere -
    # this file is the entire integration.
    #
    # command is a real shell command, not a Python import - textual-
    # serve launches a fresh subprocess per browser tab/connection,
    # the same "one player, one client.main process" model the plain
    # terminal launch already has (client/main.py, `python -m
    # client.main`), just reached through a browser instead of a local
    # terminal. Each tab gets its own real LoginScreen, same as
    # launching the terminal client fresh - there is no shared identity
    # between tabs, matching that a browser tab has no local .player_id-
    # equivalent file to begin with (the real reason server-owned
    # identity, ROADMAP.md 2026-08-13/14/15, was built first).
    command = "python -m client.main"

    host = os.environ.get("WEB_HOST", "localhost")
    port = int(os.environ.get("WEB_PORT", "8000"))
    # public_url lets a reverse-proxied deployment (e.g. behind nginx/
    # Caddy on a real domain) advertise its real external URL instead
    # of the bind address - unset by default, the same "only ask for
    # what a genuinely proxied setup needs" scoping SERVER_HOST/
    # SERVER_PORT (server/main.py) already follow.
    public_url = os.environ.get("WEB_PUBLIC_URL") or None

    server = Server(command, host=host, port=port, title="Oracle", public_url=public_url)
    server.serve()


if __name__ == "__main__":
    main()
