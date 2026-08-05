from __future__ import annotations

import asyncio
import logging
import uuid

from .engine import Broadcast, GameEngine, SendTo
from .narrator import create_narrator
from .state import Session
from .transport import Transport


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    session = Session(session_id=str(uuid.uuid4()))
    dm = create_narrator()

    def engine_factory(broadcast: Broadcast, send_to: SendTo) -> GameEngine:
        return GameEngine(session, dm, broadcast, send_to)

    transport = Transport(engine_factory)
    asyncio.run(transport.serve())


if __name__ == "__main__":
    main()
