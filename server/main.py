from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .accounts import AccountStore
from .engine import Broadcast, GameEngine, SendTo
from .narrator import create_narrator
from .persistence import JSONFileSessionStore, SessionStoreUnwritable
from .state import Session
from .transport import Transport


def main() -> None:
    # README's setup flow ("cp .env.example .env", then fill it in) only
    # works if something actually reads .env - nothing did, so DM_BACKEND/
    # ANTHROPIC_API_KEY/etc. silently fell back to os.environ's (unset)
    # values every time, regardless of what .env said. load_dotenv() does
    # NOT override real, already-exported env vars by default, so this is
    # additive: .env fills gaps, a real `export` still wins.
    load_dotenv()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    store_dir = Path(os.environ.get("SESSION_STORE_DIR", "sessions"))
    try:
        store = JSONFileSessionStore(store_dir)
    except SessionStoreUnwritable as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    dm = create_narrator()

    # One process now serves any number of concurrent games - each
    # session_id gets its own Session loaded (or created fresh) the first
    # time a client actually joins it, not one fixed session picked at
    # startup. dm is stateless (just a client/model/rules holder, no
    # per-session data - see server/narrator.py), so every session's
    # GameEngine sharing the one instance is safe.
    def engine_factory(session_id: str, broadcast: Broadcast, send_to: SendTo) -> GameEngine:
        session = store.load(session_id) or Session(session_id=session_id)
        return GameEngine(session, dm, broadcast, send_to, store=store)

    # Real server-owned identity (ROADMAP.md, 2026-08-13) - a real,
    # persistent AccountStore, not the ephemeral in-memory default
    # Transport falls back to when none is given (that default exists
    # purely so tests never risk writing a stray real accounts file).
    accounts_path = Path(os.environ.get("ACCOUNTS_FILE", "accounts.json"))
    accounts = AccountStore(accounts_path)

    transport = Transport(engine_factory, accounts=accounts)
    host = os.environ.get("SERVER_HOST", "localhost")
    port = int(os.environ.get("SERVER_PORT", "8765"))
    asyncio.run(transport.serve(host=host, port=port))


if __name__ == "__main__":
    main()
