from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .engine import Broadcast, GameEngine, SendTo
from .narrator import create_narrator
from .persistence import JSONFileSessionStore, SessionStoreUnwritable
from .state import Session
from .transport import Transport


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    session_id = os.environ.get("SESSION_ID", "default")
    store_dir = Path(os.environ.get("SESSION_STORE_DIR", "sessions"))
    try:
        store = JSONFileSessionStore(store_dir)
    except SessionStoreUnwritable as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    session = store.load(session_id) or Session(session_id=session_id)

    dm = create_narrator()

    def engine_factory(broadcast: Broadcast, send_to: SendTo) -> GameEngine:
        return GameEngine(session, dm, broadcast, send_to, store=store)

    transport = Transport(engine_factory)
    asyncio.run(transport.serve())


if __name__ == "__main__":
    main()
