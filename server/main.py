from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .engine import Broadcast, GameEngine, SendTo
from .narrator import create_narrator
from .persistence import JSONFileSessionStore
from .state import Session
from .transport import Transport


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    session_id = os.environ.get("SESSION_ID", "default")
    store = JSONFileSessionStore(Path(os.environ.get("SESSION_STORE_DIR", "sessions")))
    session = store.load(session_id) or Session(session_id=session_id)

    dm = create_narrator()

    def engine_factory(broadcast: Broadcast, send_to: SendTo) -> GameEngine:
        return GameEngine(session, dm, broadcast, send_to, store=store)

    transport = Transport(engine_factory)
    asyncio.run(transport.serve())


if __name__ == "__main__":
    main()
