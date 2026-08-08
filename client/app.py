from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from shared.protocol import Envelope

from .transport import ClientTransport

# Mirrors server/engine.py's CLASS_STARTING_EQUIPMENT keys - the SRD dataset
# (server/rules/srd.json) is the real source of truth for what a class
# grants; this is just the same short list for the prompt. An unrecognized
# or blank entry falls back gracefully server-side, so this isn't strictly
# validated here.
CHARACTER_CLASSES = ["fighter", "wizard", "rogue", "cleric"]


class CharacterSheetPanel(Static):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._character: dict = {}
        self._world: dict = {}
        self._others: dict = {}

    def render_sheet(self, character: dict) -> None:
        self._character = character
        self._refresh_display()

    def render_world(self, world: dict) -> None:
        self._world = world
        self._refresh_display()

    def render_others(self, others: dict) -> None:
        # others is keyed by player_id -> that player's public view
        # (name/character_class/hp/max_hp/conditions - never inventory,
        # same boundary the server's own public/private split enforces).
        self._others = others
        self._refresh_display()

    def _refresh_display(self) -> None:
        # Deliberately not named _render - Textual's own Widget._render() is
        # a real internal method (returns a Visual for the content-height
        # pipeline), and a same-named subclass method silently shadows it
        # instead of erroring at definition time. That collision is exactly
        # what crashed this app on startup against Textual 8.2.8's newer
        # internals (pyproject.toml only pins textual>=0.58, no upper bound)
        # - self._render() returning None broke get_content_height()'s
        # visual.get_height() call.
        character = self._character
        name_line = f"[b]{character.get('name', '?')}[/b]"
        character_class = character.get("character_class")
        if character_class:
            name_line += f" ({character_class})"
        # A header block (identity + HP) set off by a divider, then
        # everything else below it - the same shape D&D Beyond's own sheet
        # uses (name/class up top, core combat stats grouped together and
        # visually distinct from ability scores/equipment further down) and
        # Roll20's paged sheet mirrors with its "Core Page" vs. the rest.
        # Oracle has no AC/Initiative/Speed/ability-score data yet (see
        # ROADMAP.md - not fabricating placeholder fields for those), so
        # this only carries over the *shape*: an identity+vitals header,
        # set apart from the sections below it.
        lines = [name_line, self._hp_line(character.get("hp"), character.get("max_hp")), self._DIVIDER]
        stats = character.get("stats") or {}
        if stats:
            lines.extend(f"{k}: {v}" for k, v in stats.items())
            lines.append("")
        inventory = character.get("inventory") or []
        if inventory:
            lines.append("[b]Inventory[/b]")
            lines.extend(f"- {item}" for item in inventory)
            lines.append("")
        conditions = character.get("conditions") or []
        if conditions:
            lines.append("[b]Conditions[/b]")
            lines.extend(f"- {c}" for c in conditions)
            lines.append("")

        active_objectives = [o for o in (self._world.get("objectives") or []) if o.get("status") == "active"]
        if active_objectives:
            lines.append("[b]Objectives[/b]")
            lines.extend(f"- {o['text']}" for o in active_objectives)
            lines.append("")

        if self._others:
            # "Party", not "Other Players" - the genre-standard term both
            # D&D Beyond's Campaign dashboard and Roll20's turn-order
            # tracker use for this exact "everyone else, at a glance" view,
            # as opposed to a single character's own full sheet.
            lines.append(self._DIVIDER)
            lines.append("[b]Party[/b]")
            lines.extend(self._other_player_line(other) for other in self._others.values())

        self.update("\n".join(lines).rstrip())

    _DIVIDER = "[dim]" + "─" * 24 + "[/dim]"

    @staticmethod
    def _hp_bar(hp: int | None, max_hp: int | None, width: int = 10) -> str:
        # The green/yellow/red-by-fraction health bar almost every VTT uses
        # for token HP at a glance (Roll20's token bars chief among them) -
        # adapted here to plain block characters since this is a terminal
        # UI with no bar widgets/images to draw on.
        hp = hp or 0
        max_hp = max_hp or 0
        fraction = (hp / max_hp) if max_hp else 0
        filled = max(0, min(width, round(fraction * width)))
        bar = "█" * filled + "░" * (width - filled)
        color = "green" if fraction > 0.5 else "yellow" if fraction > 0.25 else "red"
        return f"[{color}]{bar}[/{color}]"

    @classmethod
    def _hp_line(cls, hp: int | None, max_hp: int | None) -> str:
        return f"HP {hp or 0}/{max_hp or 0}  {cls._hp_bar(hp, max_hp)}"

    @classmethod
    def _other_player_line(cls, other: dict) -> str:
        line = f"- {other.get('name', '?')}"
        character_class = other.get("character_class")
        if character_class:
            line += f" ({character_class})"
        hp, max_hp = other.get("hp"), other.get("max_hp")
        # A shorter bar than the main sheet's own - a party glance is meant
        # to be compact, the same "your own sheet gets full detail, the
        # party list gets a quick read" split Roll20's turn-order tracker
        # (small per-token bars) makes relative to your own character sheet.
        line += f": HP {hp}/{max_hp} {cls._hp_bar(hp, max_hp, width=6)}"
        conditions = other.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line


def _npc_status_line(name: str, npc: dict) -> str:
    line = f"[dim]{name}: HP {npc.get('hp')}/{npc.get('max_hp')}"
    conditions = npc.get("conditions") or []
    if conditions:
        line += f" ({', '.join(conditions)})"
    return line + "[/dim]"


class WelcomeScreen(Screen):
    """The client's very first screen - collects identity (name, session
    ID, and, for a genuinely new local player, class) and joins. Replaces
    client/main.py's old blocking input() prompts, which can't run inside
    a live Textual event loop anyway - same information, gathered as real
    widgets instead of stdin lines."""

    CSS = """
    WelcomeScreen { align: center middle; }
    #welcome-box { width: 50; height: auto; border: solid $accent; padding: 1 2; }
    #welcome-box Input { margin-bottom: 1; }
    #welcome-error { color: $error; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="welcome-box"):
            yield Static("[b]Oracle[/b] - an AI Dungeon Master\n")
            yield Input(placeholder="Character name", id="name-input")
            yield Input(placeholder="Session ID (blank for default)", id="session-input")
            if self.app.is_new_character:
                yield Static(f"Class ({'/'.join(CHARACTER_CLASSES)}, blank to skip)")
                yield Input(placeholder="Class", id="class-input")
            yield Button("Join", id="join", variant="primary")
            yield Static("", id="welcome-error")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "join":
            await self._join()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._join()

    async def _join(self) -> None:
        join_button = self.query_one("#join", Button)
        if join_button.disabled:
            return  # a real Enter-then-click double-submit shouldn't send join_session twice
        join_button.disabled = True

        name = self.query_one("#name-input", Input).value.strip() or "Adventurer"
        session_id = self.query_one("#session-input", Input).value.strip() or "default"
        character_class = ""
        if self.app.is_new_character:
            character_class = self.query_one("#class-input", Input).value.strip()

        try:
            await self.app.connect_and_join(name, session_id, character_class)
        except OSError as exc:
            join_button.disabled = False
            self.query_one("#welcome-error", Static).update(f"[red]Couldn't connect: {exc}[/red]")


class LobbyScreen(Screen):
    """The pre-game menu: review your own character, see who else has
    joined (Party), chat before the adventure begins, and trigger the real
    start. Owner's own framing: "should there be a main menu first where
    players can join, chat, create or load their character and or review
    their character... then when start dm begins narrating the scene."""

    CSS = """
    LobbyScreen Horizontal { height: 1fr; }
    LobbyScreen CharacterSheetPanel { width: 30%; border: solid $accent; padding: 1; }
    LobbyScreen RichLog { width: 70%; border: solid $accent; }
    #lobby-status { height: 1; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield CharacterSheetPanel(id="sheet")
            yield RichLog(id="chat-log", wrap=True, markup=True)
        yield Static("", id="lobby-status")
        yield Button("Start Adventure", id="start", variant="success")
        yield Input(placeholder="Chat with the party before starting...", id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        self.app.refresh_sheet_widgets()
        self.query_one("#chat-input", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.query_one("#start", Button).disabled = True
            self.query_one("#lobby-status", Static).update("[dim]Starting the adventure...[/dim]")
            await self.app.transport.send("start_session", {})

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            await self.app.transport.send("chat_message", {"text": text})


class SessionScreen(Screen):
    """The real in-session view - narrative log, character sheet/party,
    and the turn input. Everything client/app.py's App used to compose
    directly before the lobby existed; unchanged in content, just moved
    into its own Screen, pushed once session_started arrives."""

    CSS = """
    SessionScreen Horizontal { height: 1fr; }
    SessionScreen CharacterSheetPanel { width: 30%; border: solid $accent; padding: 1; }
    SessionScreen RichLog { width: 70%; border: solid $accent; }
    #status { height: 1; padding: 0 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._narration_buffer = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield CharacterSheetPanel(id="sheet")
            yield RichLog(id="log", wrap=True, markup=True)
        yield Static("", id="status")
        yield Input(placeholder="What do you do? (/roll 1d20, /chat hello)", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.app.refresh_sheet_widgets()
        log = self.query_one("#log", RichLog)
        for name, npc in self.app.npcs.items():
            log.write(_npc_status_line(name, npc))
        for entry in self.app.log_tail:
            log.write(entry.get("text", ""))
        self.query_one("#input", Input).focus()

    def set_thinking(self, thinking: bool) -> None:
        self.query_one("#status", Static).update("[dim]The DM is thinking...[/dim]" if thinking else "")

    def write_log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def handle_narration_chunk(self, text: str, done: bool) -> None:
        self.set_thinking(False)  # the silent gap this fills ends the moment real text starts arriving
        if done:
            self.write_log(self._narration_buffer)
            self._narration_buffer = ""
        else:
            self._narration_buffer += text

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/roll "):
            dice, _, reason = text[len("/roll "):].strip().partition(" ")
            await self.app.transport.send("dice_roll", {"dice": dice, "reason": reason})
        elif text.startswith("/chat "):
            await self.app.transport.send("chat_message", {"text": text[len("/chat "):].strip()})
        else:
            # Only a real player_action triggers a DM narrate() call - /roll
            # and /chat are handled instantly server-side with no LLM in the
            # loop, so there's nothing to wait on for those.
            self.set_thinking(True)
            await self.app.transport.send("player_action", {"text": text})


class DungeonMasterApp(App):
    """Holds session state as plain attributes (mirroring this workspace's
    own established TUI pattern for a multi-screen app - state lives on the
    App, each screen reads/writes self.app.* directly) and routes incoming
    envelopes to whichever screen is currently active. compose() is
    deliberately left as the base App's no-op: WelcomeScreen owns the
    entire first view, pushed from on_mount()."""

    def __init__(self, uri: str, player_id: str, is_new_character: bool):
        super().__init__()
        self._uri = uri
        self._player_id = player_id
        self.is_new_character = is_new_character
        self.transport: ClientTransport | None = None
        self.my_character: dict = {}
        self.others: dict[str, dict] = {}  # player_id -> public view, keyed the same way the server sends it
        self.world: dict = {}
        self.npcs: dict = {}
        self.log_tail: list[dict] = []
        self._listening = False

    async def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())

    async def connect_and_join(self, player_name: str, session_id: str, character_class: str) -> None:
        self.transport = ClientTransport(self._uri, session_id, self._player_id)
        await self.transport.connect()
        if not self._listening:
            self._listening = True
            asyncio.create_task(self._listen())
        await self.transport.send(
            "join_session", {"player_name": player_name, "character_class": character_class}
        )

    async def _listen(self) -> None:
        async for envelope in self.transport.messages():
            await self._handle(envelope)

    def _try_query_one(self, selector: str, expect_type: type):
        # self.screen, not self.query_one() - a real, non-obvious Textual
        # gotcha found by actually running this, not by reading docs:
        # App._on_compose pins App.query_one()'s search target
        # (default_screen/_compose_screen) to whatever screen existed the
        # moment App.compose() first ran, permanently - it never tracks
        # later push_screen()/switch_screen() calls. self.screen (top of
        # the real screen stack) is what actually reflects the currently
        # active screen.
        try:
            return self.screen.query_one(selector, expect_type)
        except NoMatches:
            return None

    def refresh_sheet_widgets(self) -> None:
        # Safe to call regardless of which screen is currently active or
        # whether it's even mounted yet - a no-op if #sheet doesn't exist
        # in the current screen (e.g. WelcomeScreen, which has none).
        sheet = self._try_query_one("#sheet", CharacterSheetPanel)
        if sheet is None:
            return
        sheet.render_sheet(self.my_character)
        sheet.render_others(self.others)
        sheet.render_world(self.world)

    async def _handle(self, envelope: Envelope) -> None:
        # async, and _listen() awaits each call in turn (not fire-and-
        # forget) specifically so the push_screen/switch_screen awaits
        # below can matter - a real race, found only by running this
        # against a real server, not by reading the code: push_screen()
        # schedules mounting the new screen's widgets asynchronously, and
        # without awaiting it, the very next queued envelope (routinely
        # the "X joined the session" system_message broadcast, sent right
        # after state_sync in _on_join_session) could already be dispatched
        # and try to query a widget (e.g. #lobby-status) that doesn't exist
        # in the DOM yet, crashing with NoMatches.
        if envelope.type == "state_sync":
            payload = envelope.payload
            characters = payload.get("characters", {})
            self.my_character = characters.get(self._player_id, {})
            self.others = {pid: c for pid, c in characters.items() if pid != self._player_id}
            self.world = payload.get("world_state", {})
            self.npcs = payload.get("npcs", {})
            self.log_tail = payload.get("log_tail", [])

            if isinstance(self.screen, WelcomeScreen):
                # started: whether the adventure has already begun (see
                # GameEngine._has_started()) - a fresh join lands in the
                # lobby, a reconnect into an already-started game skips
                # straight to the real session view.
                if payload.get("started"):
                    await self.push_screen(SessionScreen())
                else:
                    await self.push_screen(LobbyScreen())
            else:
                self.refresh_sheet_widgets()
            return

        if envelope.type == "character_update":
            if envelope.payload.get("player_id") == self._player_id:
                self.my_character = envelope.payload.get("sheet_delta", {})
                self.refresh_sheet_widgets()
            return

        if envelope.type in ("player_joined", "player_update"):
            pid = envelope.payload.get("player_id")
            if pid and pid != self._player_id:
                self.others[pid] = envelope.payload
                self.refresh_sheet_widgets()
            return

        if envelope.type == "player_left":
            pid = envelope.payload.get("player_id")
            if self.others.pop(pid, None) is not None:
                self.refresh_sheet_widgets()
            return

        if envelope.type == "session_started":
            if isinstance(self.screen, LobbyScreen):
                await self.switch_screen(SessionScreen())
            return

        if envelope.type == "world_update":
            self.world = envelope.payload
            self.refresh_sheet_widgets()
            return

        # Everything below only ever matters once the real session view
        # exists - a lobby-phase chat_message is the one exception, handled
        # separately since it targets #chat-log, not #log.
        session_screen = self.screen if isinstance(self.screen, SessionScreen) else None

        if envelope.type == "npc_update":
            if session_screen is not None:
                name = envelope.payload.get("name", "?")
                session_screen.write_log(_npc_status_line(name, envelope.payload.get("sheet_delta", {})))
            return

        if envelope.type == "log_entry":
            text = envelope.payload.get("text", "")
            kind = envelope.payload.get("kind")
            if session_screen is not None and kind == "narration":
                session_screen.handle_narration_chunk(text, bool(envelope.payload.get("done")))
            elif session_screen is not None:
                session_screen.write_log(text)
            elif kind == "chat" and isinstance(self.screen, LobbyScreen):
                self.screen.query_one("#chat-log", RichLog).write(text)
            return

        if envelope.type == "turn_prompt":
            # A real, previously-unexercised multiplayer gap, found only by
            # actually running two real clients through a real session
            # (see ROADMAP.md): turn_prompt broadcasts to everyone, but
            # this used to only ever write something for the player whose
            # turn it now is - anyone else got no indication at all of
            # whose turn it was, not even at session start. Every other
            # player's public view (name included) is already tracked in
            # self.others from player_joined/state_sync, so this needs no
            # new protocol field - just using data already on hand.
            if session_screen is not None:
                turn_player_id = envelope.payload.get("player_id")
                if turn_player_id == self._player_id:
                    session_screen.write_log("[i]Your turn.[/i]")
                else:
                    name = self.others.get(turn_player_id, {}).get("name", "Someone")
                    session_screen.write_log(f"[i]{name}'s turn.[/i]")
            return

        if envelope.type == "system_message":
            text = envelope.payload.get("text", "")
            if session_screen is not None:
                session_screen.set_thinking(False)  # covers narration-failed, which never reaches a log_entry
                session_screen.write_log(f"[dim]{text}[/dim]")
            elif isinstance(self.screen, LobbyScreen):
                self.screen.query_one("#lobby-status", Static).update(f"[dim]{text}[/dim]")
            return
