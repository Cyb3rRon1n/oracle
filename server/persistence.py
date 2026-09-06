from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .state import Session


class SessionStore(Protocol):
    def load(self, session_id: str) -> Session | None:
        """Return the saved session, or None if nothing is saved yet."""

    def save(self, session: Session) -> None:
        """Persist the session's current state."""


class SessionStoreUnwritable(RuntimeError):
    """Raised at startup when a SessionStore's backing directory can't be
    written to - e.g. missing, wrong permissions, or (the incident that
    prompted this check) a relative SESSION_STORE_DIR resolved against a
    process cwd that no longer exists after the repo it was launched from
    got moved/renamed out from under it. Meant to fail loudly at boot
    instead of every subsequent turn's save() silently raising into an
    unhandled exception that just kills that connection - see ROADMAP.md."""


class JSONFileSessionStore:
    """One JSON file per session_id. Swap for a database-backed SessionStore
    later without touching the engine, same pattern as NarratorBackend."""

    def __init__(self, directory: Path):
        self._directory = directory
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            probe = self._directory / ".write_check"
            probe.write_text("")
            probe.unlink()
        except OSError as exc:
            raise SessionStoreUnwritable(
                f"Session store directory {str(self._directory)!r} is not writable ({exc}). "
                "Game state cannot be saved - check SESSION_STORE_DIR and its permissions."
            ) from exc

    def _path(self, session_id: str) -> Path:
        return self._directory / f"{session_id}.json"

    def load(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return Session.model_validate_json(path.read_text())
        except ValueError:
            # A truncated/corrupt file (crash mid-write on the pre-atomic
            # code path, manual edit, disk error). Fall back to the last
            # good copy rather than crashing every join for this session.
            backup = path.with_suffix(".json.bak")
            if backup.exists():
                return Session.model_validate_json(backup.read_text())
            raise

    def save(self, session: Session) -> None:
        # Atomic: write a temp file in the same directory, fsync, then
        # os.replace() it over the target - a crash or full disk mid-write
        # can't leave a half-written session.json. The previous good file
        # is kept as <id>.json.bak for load()'s fallback.
        path = self._path(session.session_id)
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(session.model_dump_json(indent=2))
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        if path.exists():
            os.replace(path, path.with_suffix(".json.bak"))
        os.replace(tmp, path)
