from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
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


class Account(BaseModel):
    username: str
    player_id: str
    password_hash: str
    salt: str  # hex-encoded, not raw bytes - JSON has no bytes type


@dataclass
class AuthResult:
    """Plain result, never raises - the same "manager function returns a
    result dict/dataclass" shape every other user-facing action in this
    codebase already follows (CharacterSheet.apply_update, dice.roll)."""

    success: bool
    player_id: str | None = None
    is_new_account: bool = False
    error: str | None = None


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
        return AuthResult(success=True, player_id=existing.player_id, is_new_account=False)
