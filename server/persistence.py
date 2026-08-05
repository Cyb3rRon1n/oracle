from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .state import Session


class SessionStore(Protocol):
    def load(self, session_id: str) -> Session | None:
        """Return the saved session, or None if nothing is saved yet."""

    def save(self, session: Session) -> None:
        """Persist the session's current state."""


class JSONFileSessionStore:
    """One JSON file per session_id. Swap for a database-backed SessionStore
    later without touching the engine, same pattern as NarratorBackend."""

    def __init__(self, directory: Path):
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._directory / f"{session_id}.json"

    def load(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        return Session.model_validate_json(path.read_text())

    def save(self, session: Session) -> None:
        path = self._path(session.session_id)
        path.write_text(session.model_dump_json(indent=2))
