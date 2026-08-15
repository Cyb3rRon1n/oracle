from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

# PBKDF2-HMAC-SHA256, stdlib only (hashlib/secrets) - no new dependency,
# matching this project's existing low-dependency posture. 600_000
# iterations is OWASP's current (2023+) minimum recommendation for
# PBKDF2-SHA256 - not scaled down for "just a hobby project," since the
# real cost (one hash per login attempt, never per-turn) is negligible
# either way.
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS).hex()


# Capped, most-recent-first - the Main Menu hub (ROADMAP.md, 2026-08-15)
# only ever needs "the last few tables you were at" to offer a real
# Continue list, not a full unbounded history; capping keeps the account
# record itself small regardless of how many sessions a player has ever
# joined.
_MAX_RECENT_SESSIONS = 10


class Account(BaseModel):
    username: str
    player_id: str
    password_hash: str
    salt: str  # hex-encoded, not raw bytes - JSON has no bytes type
    # Which session_ids this player_id has actually joined, most-recent-
    # first - the real, server-owned answer to "what tables can I
    # continue," replacing the old client-local-file-only reconnect
    # story (ROADMAP.md, 2026-08-15's Main Menu hub). A session_id
    # appearing here is not the character itself (that still lives on
    # the session's own Session.characters[player_id], server/state.py)
    # - just a pointer to where a real one might already exist, so the
    # Main Menu can offer it without inventing a second character store.
    recent_sessions: list[str] = []


@dataclass
class AuthResult:
    """Plain result, never raises - the same "manager function returns a
    result dict/dataclass" shape every other user-facing action in this
    codebase already follows (CharacterSheet.apply_update, dice.roll)."""

    success: bool
    player_id: str | None = None
    is_new_account: bool = False
    error: str | None = None
    recent_sessions: list[str] = field(default_factory=list)


class AccountStore:
    """One JSON file holding every account, keyed by username - real
    server-owned identity replacing the client-local .player_id file
    (ROADMAP.md, 2026-08-13: a client-supplied player_id was previously
    trusted outright, with no login at all). First login for a username
    creates the account - the simplest real auth flow for a small
    self-hosted game: no separate "register" step, no email or external
    provider, matching the recommendation already written into
    ROADMAP.md. Every later login for that same username must match the
    original password. A returning username always gets back the exact
    same player_id it was issued on first login, preserving the
    "reconnect as the same character" property the old .player_id file
    used to provide - just proven by a password instead of read from a
    local file, so it now also works from a different machine/browser."""

    def __init__(self, path: Path | None = None):
        # path=None is a genuine in-memory-only store, not a file that
        # happens to never get written - no disk I/O occurs at all in
        # this mode. This is Transport's own default when no AccountStore
        # is explicitly supplied (server/transport.py), specifically so
        # every existing test/call site that constructs a bare Transport
        # never risks writing a stray real accounts file to disk - the
        # same class of test-pollution bug (a stray real file bleeding
        # between test runs) this project's sibling repos have hit
        # repeatedly and documented explicitly.
        self._path = path
        self._memory: dict[str, Account] = {}
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> dict[str, Account]:
        if self._path is None:
            return dict(self._memory)
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text())
        return {username: Account.model_validate(data) for username, data in raw.items()}

    def _save_all(self, accounts: dict[str, Account]) -> None:
        if self._path is None:
            self._memory = dict(accounts)
            return
        self._path.write_text(json.dumps({u: a.model_dump() for u, a in accounts.items()}, indent=2))

    def authenticate(self, username: str, password: str) -> AuthResult:
        username = username.strip()
        if not username or not password:
            return AuthResult(success=False, error="Username and password are both required.")

        accounts = self._load_all()
        existing = accounts.get(username)

        if existing is None:
            salt = secrets.token_bytes(_SALT_BYTES)
            account = Account(
                username=username,
                player_id=str(uuid.uuid4()),
                password_hash=_hash_password(password, salt),
                salt=salt.hex(),
            )
            accounts[username] = account
            self._save_all(accounts)
            return AuthResult(success=True, player_id=account.player_id, is_new_account=True)

        salt = bytes.fromhex(existing.salt)
        candidate_hash = _hash_password(password, salt)
        # hmac.compare_digest, not `==` - a constant-time comparison so a
        # login attempt can't be timed to leak how many leading hex
        # characters of the real hash it got right.
        if not hmac.compare_digest(candidate_hash, existing.password_hash):
            return AuthResult(success=False, error="Incorrect password.")
        return AuthResult(
            success=True, player_id=existing.player_id, is_new_account=False,
            recent_sessions=list(existing.recent_sessions),
        )

    def record_session_joined(self, player_id: str, session_id: str) -> None:
        """Called once a real join_session actually succeeds for this
        player_id (server/transport.py) - moves session_id to the front
        of that account's recent_sessions, deduplicating and capping at
        _MAX_RECENT_SESSIONS. A linear scan for the matching player_id,
        not a second player_id->username index - account counts on a
        small self-hosted game are tiny, the same "don't build a second
        data structure until a real case forces it" reasoning this
        project already applies elsewhere. Silently a no-op if no
        account matches (shouldn't happen in practice - player_id only
        ever comes from a real prior login - but this is bookkeeping,
        not a security boundary, so it fails soft rather than raising
        into the connection's own message loop)."""
        accounts = self._load_all()
        for username, account in accounts.items():
            if account.player_id != player_id:
                continue
            recent = [sid for sid in account.recent_sessions if sid != session_id]
            recent.insert(0, session_id)
            accounts[username] = account.model_copy(update={"recent_sessions": recent[:_MAX_RECENT_SESSIONS]})
            self._save_all(accounts)
            return
