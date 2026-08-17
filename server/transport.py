from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from websockets.asyncio.server import ServerConnection, serve

from shared.protocol import Envelope

from .accounts import AccountStore
from .engine import Broadcast, GameEngine, SendTo

logger = logging.getLogger(__name__)

EngineFactory = Callable[[str, Broadcast, SendTo], GameEngine]


class Transport:
    """One process, many concurrent games: each session_id gets its own
    GameEngine, created lazily on that session's first join_session and
    reused afterward (session_id -> GameEngine). Connections are tracked
    per player_id but each carries its own session_id alongside, so a
    broadcast for one session's engine only reaches that session's own
    connections - two unrelated games sharing a server never see each
    other's traffic. Previously there was exactly one GameEngine for the
    whole process (loaded from a single startup SESSION_ID env var), so
    every client that connected joined the same game regardless of what
    session_id it actually sent - a client-specified "join a different
    game" was silently ignored.

    accounts (server/accounts.py) makes identity itself server-owned
    (ROADMAP.md, 2026-08-13) - a connection must send a real login before
    anything else, and every envelope after that is checked against the
    identity that login actually proved, not just trusted from whatever
    sender_id the client happens to put in the envelope. Optional and
    defaulted to an in-memory-only AccountStore (no real file ever
    written) so every existing test/call site that constructs a
    Transport with just an engine_factory keeps working unchanged with
    zero risk of a stray real accounts file bleeding between test runs -
    only server/main.py's real production wiring passes a real,
    persistent one."""

    def __init__(self, engine_factory: EngineFactory, accounts: AccountStore | None = None):
        self._engine_factory = engine_factory
        self._accounts = accounts if accounts is not None else AccountStore()
        self._engines: dict[str, GameEngine] = {}
        # player_id -> (session_id, connection)
        self._connections: dict[str, tuple[str, ServerConnection]] = {}
        # The Tavern lobby (ROADMAP.md, 2026-08-15) - player_id ->
        # connection, for a connection that has logged in but not yet
        # joined a real session (or has left one - see _handler's finally
        # block). Genuinely separate from self._connections above: that
        # dict only ever tracks connections *inside* a real session, and
        # a lobby-wide chat/directory broadcast has no session_id to key
        # on the way _broadcast(session_id, ...) already does.
        self._lobby_connections: dict[str, ServerConnection] = {}
        self._lobby_names: dict[str, str] = {}  # player_id -> username, for a real display name in tavern_message

    def _get_or_create_engine(self, session_id: str) -> GameEngine:
        engine = self._engines.get(session_id)
        if engine is None:

            async def broadcast(envelope: Envelope) -> None:
                await self._broadcast(session_id, envelope)

            engine = self._engine_factory(session_id, broadcast, self._send_to)
            self._engines[session_id] = engine
        return engine

    async def _broadcast(self, session_id: str, envelope: Envelope) -> None:
        message = envelope.to_json()
        targets = [conn for sid, conn in self._connections.values() if sid == session_id]
        await asyncio.gather(*(conn.send(message) for conn in targets), return_exceptions=True)

    async def _send_to(self, player_id: str, envelope: Envelope) -> None:
        entry = self._connections.get(player_id)
        if entry is not None:
            _, conn = entry
            await conn.send(envelope.to_json())

    def _build_tavern_directory(self) -> list[dict]:
        """Every currently-active session (one with at least one real
        connection right now) - a dormant session that only exists as a
        saved sessions/*.json file with nobody currently in it doesn't
        appear, the same "who's here right now" framing the Tavern was
        actually asked for, not a full historical archive."""
        counts: dict[str, int] = {}
        for session_id, _ in self._connections.values():
            counts[session_id] = counts.get(session_id, 0) + 1
        return [
            {"session_id": session_id, "player_count": count, "started": self._engines[session_id].is_started}
            for session_id, count in sorted(counts.items())
        ]

    async def _send_tavern_directory(self, connection: ServerConnection) -> None:
        await connection.send(
            Envelope(
                type="tavern_directory", session_id="", sender_id="server",
                payload={"sessions": self._build_tavern_directory()},
            ).to_json()
        )

    async def _broadcast_tavern_directory(self) -> None:
        """Called whenever a real session's player count or started
        state could have changed (a join, a disconnect) - a no-op if
        nobody is currently in the Tavern to show it to."""
        if not self._lobby_connections:
            return
        message = Envelope(
            type="tavern_directory", session_id="", sender_id="server",
            payload={"sessions": self._build_tavern_directory()},
        ).to_json()
        await asyncio.gather(
            *(conn.send(message) for conn in self._lobby_connections.values()), return_exceptions=True
        )

    async def _handler(self, connection: ServerConnection) -> None:
        player_id: str | None = None
        session_id: str | None = None
        # The real identity this connection has actually proven via login
        # - not the same thing as `player_id` above, which is only ever
        # set once join_session arrives (see its own comment). None until
        # a real login succeeds on this connection; nothing past login
        # itself is dispatched anywhere until it's set.
        authenticated_player_id: str | None = None
        try:
            async for raw in connection:
                envelope = Envelope.from_json(raw)

                if envelope.type == "login":
                    username = envelope.payload.get("username", "")
                    result = self._accounts.authenticate(username, envelope.payload.get("password", ""))
                    authenticated_player_id = result.player_id if result.success else None
                    await connection.send(
                        Envelope(
                            type="login_result",
                            session_id="",
                            sender_id="server",
                            payload={
                                "success": result.success,
                                "player_id": result.player_id,
                                "is_new_account": result.is_new_account,
                                "error": result.error,
                                "recent_sessions": result.recent_sessions,
                            },
                        ).to_json()
                    )
                    if result.success and authenticated_player_id is not None:
                        # The Tavern lobby (ROADMAP.md, 2026-08-15) - a
                        # freshly logged-in connection hasn't joined a
                        # real session yet, so it starts out "in the
                        # Tavern": tracked for lobby-wide chat and given
                        # an immediate real directory snapshot of what's
                        # currently active, before it's chosen anything.
                        self._lobby_connections[authenticated_player_id] = connection
                        self._lobby_names[authenticated_player_id] = username.strip()
                        await self._send_tavern_directory(connection)
                    continue

                # Real server-owned identity, not a client-asserted one:
                # every envelope past login must carry the exact sender_id
                # that login actually proved for this connection - a
                # client can no longer just claim to be anyone by setting
                # sender_id to whatever it likes. Refuses cleanly with a
                # real system_message rather than silently trusting it or
                # crashing.
                if authenticated_player_id is None or envelope.sender_id != authenticated_player_id:
                    await connection.send(
                        Envelope(
                            type="system_message",
                            session_id=envelope.session_id,
                            sender_id="server",
                            payload={"level": "error", "text": "Not authenticated - log in first."},
                        ).to_json()
                    )
                    continue

                if envelope.type == "tavern_chat":
                    # Only a connection genuinely still "in the Tavern"
                    # (not yet at a real table) can speak here - the same
                    # boundary a session's own turn queue already
                    # enforces for player_action, just for lobby-wide
                    # chat instead of one table's own chat_message.
                    if authenticated_player_id in self._lobby_connections:
                        text = envelope.payload.get("text", "")
                        name = self._lobby_names.get(authenticated_player_id, "?")
                        message = Envelope(
                            type="tavern_message", session_id="", sender_id="server",
                            payload={"player_id": authenticated_player_id, "name": name, "text": text},
                        ).to_json()
                        await asyncio.gather(
                            *(conn.send(message) for conn in self._lobby_connections.values()),
                            return_exceptions=True,
                        )
                    continue

                if envelope.type == "join_session":
                    player_id = envelope.sender_id
                    session_id = envelope.session_id
                    self._connections[player_id] = (session_id, connection)
                    # No longer "in the Tavern" once seated at a real
                    # table - matches this project's own established
                    # scope for this first pass (see docs/protocol.md's
                    # "Tavern lobby" section): there's no client-side
                    # "leave session, return to Main Menu" flow yet
                    # either, so a connection never needs to re-enter
                    # self._lobby_connections after this.
                    self._lobby_connections.pop(player_id, None)
                    self._lobby_names.pop(player_id, None)
                    # Real Main Menu "Continue" support (ROADMAP.md,
                    # 2026-08-15) - records this session_id against the
                    # authenticated account so a later login can offer
                    # it back, regardless of which machine/browser that
                    # later login happens from.
                    self._accounts.record_session_joined(player_id, session_id)
                if envelope.type == "leave_session":
                    # Deliberate leave (/leave command) vs. implicit
                    # disconnect - same turn_order/player_left cleanup
                    # via engine.handle_leave(), but the player's socket
                    # stays open; they re-enter the Tavern lobby and
                    # get an updated directory so they can pick a new
                    # table or just hang out.
                    leave_player_id = envelope.sender_id
                    leave_conn = self._connections.get(leave_player_id)
                    leave_session_id = leave_conn[0] if leave_conn else None
                    # handle_leave calls _send_to which reads
                    # _connections, so the player must still be there
                    # when the engine fires; popped afterward.
                    leave_engine = self._engines.get(leave_session_id) if leave_session_id else None
                    if leave_engine is not None:
                        await leave_engine.handle_leave(leave_player_id)
                    self._connections.pop(leave_player_id, None)
                    self._lobby_connections[leave_player_id] = connection
                    # Keep the name so tavern_chat still works - the
                    # lobby_names dict may have been cleared when the
                    # player first joined a session.
                    if leave_player_id not in self._lobby_names:
                        self._lobby_names[leave_player_id] = authenticated_player_id or ""
                    await self._send_tavern_directory(connection)
                    await self._broadcast_tavern_directory()
                    continue
                engine = self._get_or_create_engine(envelope.session_id)
                await engine.handle(envelope)
                if envelope.type == "join_session":
                    # After, not before, engine.handle() - the new
                    # engine (_get_or_create_engine, above) must exist
                    # first, since _build_tavern_directory() reads each
                    # active session's own engine.is_started.
                    await self._broadcast_tavern_directory()
        finally:
            if authenticated_player_id is not None:
                # A connection that disconnects while still only in the
                # Tavern (never joined a real table) just needs removing
                # here - no session's player count changed, so no
                # directory refresh is owed to anyone in that case.
                self._lobby_connections.pop(authenticated_player_id, None)
                self._lobby_names.pop(authenticated_player_id, None)
            if player_id is not None:
                self._connections.pop(player_id, None)
                # The counterpart to _on_join_session's player_joined
                # broadcast - a disconnect isn't a client-sent event, so the
                # engine can't learn about it through the normal handle()/
                # envelope dispatch path; the transport is the only thing
                # that actually observes the socket closing.
                engine = self._engines.get(session_id) if session_id is not None else None
                if engine is not None:
                    await engine.handle_disconnect(player_id)
                # A real table's player count just changed - refresh
                # whoever's still in the Tavern watching the directory.
                await self._broadcast_tavern_directory()

    async def serve(self, host: str = "localhost", port: int = 8765) -> None:
        async with serve(self._handler, host, port):
            logger.info("Server listening on ws://%s:%s", host, port)
            await asyncio.Future()
