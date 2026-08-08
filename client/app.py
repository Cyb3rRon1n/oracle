from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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


def _load_character_file(path: str) -> tuple[dict | None, str | None]:
    """Reads and parses a character export file for WelcomeScreen's optional
    import field - local file I/O, so this happens client-side before
    join_session is even sent. The server still independently validates the
    shape (server/engine.py's _character_from_import) since a client is
    never a trusted boundary for another connection's data - this is just
    the "can we even read and parse it" check, so a bad path fails fast
    with a clear message instead of silently sending garbage to the server.
    Returns (data, None) on success or (None, error_message) on any
    failure - a missing file, invalid JSON, or valid JSON that isn't even
    an object all become one user-facing message rather than a raw
    traceback, the same graceful-fallback spirit build_starting_character's
    blank/unrecognized-class handling already established server-side."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError:
        return None, f"Couldn't read '{path}' - check the path and try again."
    except json.JSONDecodeError:
        return None, f"'{path}' isn't valid JSON."
    if not isinstance(data, dict):
        return None, f"'{path}' doesn't look like a character file."
    return data, None


def _transcript_text(rich_log: RichLog) -> str:
    """Plain text of everything currently in a RichLog, one line per Strip.
    Strip.text already discards Rich markup/styling, leaving just the
    characters a player actually reads on screen - the same technique the
    test suite's own _log_text helper already relies on. RichLog's own
    max_lines defaults to unbounded (none of this client's RichLog
    instances set it), so .lines genuinely holds the whole session's
    accumulated log for the lifetime of the widget, not just what's
    currently scrolled into view."""
    return "\n".join(strip.text for strip in rich_log.lines)


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
        # "Lv N" alongside class - the same header real sheets put level in
        # (D&D Beyond shows it right next to class on the summary panel).
        # level defaults to 1 rather than being omitted when absent, since
        # every real character sheet payload carries it (server/state.py's
        # CharacterSheet.level default) - only a genuinely bare/legacy dict
        # (e.g. an NPC view, which has no level at all) would fall through
        # to the default here.
        name_line += f"  Lv {character.get('level', 1)}"
        # A header block (identity + HP/AC) set off by a divider, then
        # everything else below it - the same shape D&D Beyond's own sheet
        # uses (name/class up top, core combat stats grouped together and
        # visually distinct from ability scores/equipment further down) and
        # Roll20's paged sheet mirrors with its "Core Page" vs. the rest.
        # Oracle still has no Initiative/Speed data (see ROADMAP.md - not
        # fabricating placeholder fields for those); AC joins HP in this
        # header now, ability scores populate further below, like D&D
        # Beyond's own sheet layout.
        lines = [name_line, self._hp_line(character.get("hp"), character.get("max_hp"), character.get("ac"))]
        # xp is only ever present on the owner's own full sheet (never on
        # an "others"/party entry - server/engine.py's _public_character_view
        # deliberately keeps it private, same boundary inventory/stats/notes
        # already have), so this line only shows up on your own sheet.
        if "xp" in character:
            lines.append(f"[dim]XP: {character.get('xp', 0)}[/dim]")
        lines.append(self._DIVIDER)
        stats = character.get("stats") or {}
        if stats:
            # stat_modifiers is a server-side @computed_field (real
            # precomputed modifiers, server/state.py) - present whenever
            # stats is, so this never recomputes floor((score-10)/2)
            # client-side. A fixed str/dex/con/int/wis/cha order, not dict
            # iteration order - stats is keyed the same way SRD monster
            # blocks already are, but a client shouldn't assume any
            # particular dict insertion order survived JSON round-tripping.
            modifiers = character.get("stat_modifiers") or {}
            lines.append("[b]Ability Scores[/b]")
            lines.extend(
                f"{key.upper()} {stats[key]} ({'+' if modifiers.get(key, 0) >= 0 else ''}{modifiers.get(key, 0)})"
                for key in ("str", "dex", "con", "int", "wis", "cha")
                if key in stats
            )
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
    def _hp_line(cls, hp: int | None, max_hp: int | None, ac: int | None = None) -> str:
        # ac is optional (an NPC status line has no ac at all pre-this-
        # feature callers, and a genuinely bare/legacy dict might not
        # carry one either) - omitted rather than shown as "AC 0", a real
        # value some future all-armor-stripped character could otherwise
        # be confused with.
        ac_label = f"  AC {ac}" if ac is not None else ""
        return f"HP {hp or 0}/{max_hp or 0}  {cls._hp_bar(hp, max_hp)}{ac_label}"

    @classmethod
    def _other_player_line(cls, other: dict) -> str:
        line = f"- {other.get('name', '?')}"
        character_class = other.get("character_class")
        if character_class:
            line += f" ({character_class})"
        line += f" Lv{other.get('level', 1)}"
        hp, max_hp = other.get("hp"), other.get("max_hp")
        # A shorter bar than the main sheet's own - a party glance is meant
        # to be compact, the same "your own sheet gets full detail, the
        # party list gets a quick read" split Roll20's turn-order tracker
        # (small per-token bars) makes relative to your own character sheet.
        line += f": HP {hp}/{max_hp} {cls._hp_bar(hp, max_hp, width=6)}"
        ac = other.get("ac")
        if ac is not None:
            line += f" AC {ac}"
        conditions = other.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line


def _npc_status_line(name: str, npc: dict) -> str:
    line = f"[dim]{name}: HP {npc.get('hp')}/{npc.get('max_hp')}"
    ac = npc.get("ac")
    if ac is not None:
        line += f" AC {ac}"
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
    #welcome-box { width: 50; height: auto; max-height: 100%; border: solid $accent; padding: 0 2; }
    #welcome-box Input { margin-bottom: 1; }
    #welcome-box #import-input { margin-bottom: 0; }
    #welcome-error { color: $error; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        # VerticalScroll, not Vertical - the two new import-related widgets
        # (added alongside the existing class prompt) can push a new-
        # character join past a standard 80x24 viewport's visible height;
        # max-height: 100% above caps the box so it scrolls internally
        # instead of pushing #join off-screen, the same real fix this
        # workspace's other Textual projects have needed for the identical
        # "adding fields to an already-tall screen" class of bug.
        with VerticalScroll(id="welcome-box"):
            yield Static("[b]Oracle[/b] - an AI Dungeon Master")
            yield Input(placeholder="Character name", id="name-input")
            yield Input(placeholder="Session ID (blank for default)", id="session-input")
            if self.app.is_new_character:
                yield Static(f"Class ({'/'.join(CHARACTER_CLASSES)}, blank to skip)")
                yield Input(placeholder="Class", id="class-input")
                # Import-time only, same as class - a returning character
                # already has everything an export would carry, so there's
                # nothing to import into it. A filled-in path here makes
                # name/class above irrelevant server-side (the imported
                # sheet wins - see server/engine.py's
                # _character_from_import). No separate label Static here
                # (unlike class-input above) - the placeholder alone says
                # enough, and this screen's row budget is already tight on
                # a standard 80x24 terminal (see the VerticalScroll note
                # on #welcome-box above).
                yield Input(placeholder="Import character .json (optional, overrides name/class)", id="import-input")
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
        imported_character = None
        if self.app.is_new_character:
            character_class = self.query_one("#class-input", Input).value.strip()
            import_path = self.query_one("#import-input", Input).value.strip()
            if import_path:
                imported_character, error = _load_character_file(import_path)
                if error:
                    join_button.disabled = False
                    self.query_one("#welcome-error", Static).update(f"[red]{error}[/red]")
                    return

        try:
            await self.app.connect_and_join(name, session_id, character_class, imported_character)
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
        yield Input(placeholder="Chat with the party... (/export [file] to save your character)", id="chat-input")
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
        if not text:
            return

        if text.startswith("/export"):
            filename = text[len("/export"):].strip() or "character"
            message = await self.app.export_character(filename)
            self.query_one("#chat-log", RichLog).write(f"[dim]{message}[/dim]")
            return

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
        yield Input(
            placeholder="What do you do? (/roll, /chat, /note, /item add|remove, /export, /transcript [file])",
            id="input",
        )
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
        elif text.startswith("/note "):
            # notes/inventory bookkeeping, not adjudicated by the DM -
            # server/engine.py's _on_character_edit is the only handler,
            # exempt from turn order the same as /roll and /chat.
            await self.app.transport.send(
                "character_edit", {"field": "notes", "value": text[len("/note "):].strip()}
            )
        elif text.startswith("/item add "):
            await self.app.transport.send(
                "character_edit", {"field": "add_item", "value": text[len("/item add "):].strip()}
            )
        elif text.startswith("/item remove "):
            await self.app.transport.send(
                "character_edit", {"field": "remove_item", "value": text[len("/item remove "):].strip()}
            )
        elif text.startswith("/export"):
            filename = text[len("/export"):].strip() or "character"
            message = await self.app.export_character(filename)
            self.write_log(f"[dim]{message}[/dim]")
        elif text.startswith("/transcript"):
            # Client-side only, no protocol involved at all - the running
            # log this reads from is already everything the player has seen
            # this session, so there's nothing to ask the server for.
            filename = text[len("/transcript"):].strip() or "transcript"
            path = Path(filename)
            if path.suffix != ".txt":
                path = path.with_suffix(".txt")
            try:
                path.write_text(_transcript_text(self.query_one("#log", RichLog)))
            except OSError as exc:
                self.write_log(f"[dim]Couldn't save transcript: {exc}[/dim]")
            else:
                self.write_log(f"[dim]Transcript saved to {path}[/dim]")
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

    async def connect_and_join(
        self, player_name: str, session_id: str, character_class: str, imported_character: dict | None = None
    ) -> None:
        self.transport = ClientTransport(self._uri, session_id, self._player_id)
        await self.transport.connect()
        if not self._listening:
            self._listening = True
            asyncio.create_task(self._listen())
        payload = {"player_name": player_name, "character_class": character_class}
        if imported_character is not None:
            payload["imported_character"] = imported_character
        await self.transport.send("join_session", payload)

    async def export_character(self, filename: str) -> str:
        """Writes the current player's own full character sheet to a local
        JSON file - the client-side half of character save/load (the
        server-side half is join_session's optional imported_character
        field, see WelcomeScreen/_load_character_file above).
        self.my_character already carries everything worth saving (xp,
        level, inventory, notes, ...) since it's the exact private
        sheet_delta state_sync/character_update already deliver to their
        owner - no new protocol needed just to export it. Returns a
        user-facing status string rather than raising, matching this
        client's existing "report, don't crash the input loop" convention
        (WelcomeScreen's own OSError handling on connect)."""
        path = Path(filename)
        if path.suffix != ".json":
            path = path.with_suffix(".json")
        try:
            path.write_text(json.dumps(self.my_character, indent=2))
        except OSError as exc:
            return f"Couldn't export: {exc}"
        return f"Character exported to {path}"

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

    def _roller_name(self, roller_id: str) -> str:
        # dice_result's roller_id is always a real player_id - both
        # dice_roll (player-initiated) and request_roll (DM-initiated,
        # engine.py's _narrate_and_apply) are always attributed to the
        # acting character, never an NPC.
        if roller_id == self._player_id:
            return self.my_character.get("name") or "You"
        return self.others.get(roller_id, {}).get("name", "Someone")

    def _dice_result_line(self, payload: dict) -> str:
        name = self._roller_name(payload.get("roller_id"))
        notation = payload.get("dice", "")
        total = payload.get("result")
        rolls = payload.get("rolls") or []
        sides = payload.get("sides")
        purpose = payload.get("purpose")
        label = f" ({purpose})" if purpose else ""

        # damage_type only ever appears on a DM-requested weapon damage
        # roll (server/engine.py's request_roll closure resolving a real
        # weapon field) - shown right after the die itself, matching the
        # server's own tool_result wording ("1d8 (slashing)").
        damage_type = payload.get("damage_type")
        if damage_type:
            notation = f"{notation} ({damage_type})"

        # ability/ability_modifier only ever appear on a DM-requested roll
        # tied to one of the character's own ability scores
        # (server/engine.py's request_roll closure) - shown as its own
        # "+N ABIL" tag so the total's makeup stays visible, not folded
        # invisibly into a total that otherwise wouldn't explain itself.
        ability_mod = payload.get("ability_modifier")
        if ability_mod is not None:
            sign = "+" if ability_mod >= 0 else ""
            notation = f"{notation} {sign}{ability_mod} {payload['ability'].upper()}"

        # disadvantage_reasons only ever appears on a request_roll the
        # engine automatically rolled with disadvantage (server/engine.py's
        # _has_disadvantage, triggered by a tracked condition like
        # poisoned/frightened/prone) - names the real reason so it's not
        # just a bare, unexplained "why did that come out low".
        disadvantage_reasons = payload.get("disadvantage_reasons")
        if disadvantage_reasons:
            notation = f"{notation} (disadvantage: {', '.join(disadvantage_reasons)})"

        # Highlight a natural max (a "20" on a d20, but generalized to
        # whatever die was actually rolled) or a natural min the same way -
        # exactly the "highlighting a natural 20" example
        # docs/protocol.md's own known-gap note named as the payoff for
        # handling this envelope at all. With disadvantage, `rolls` holds
        # both d20s but only the *worse* one was actually kept - checking
        # either raw entry would wrongly highlight green off a high die
        # that got discarded, so the check narrows to the kept die when
        # disadvantage applied, while still displaying both rolls below.
        highlight_rolls = rolls
        if payload.get("disadvantage") and len(rolls) == 2:
            highlight_rolls = [min(rolls)]
        rolls_text = str(rolls)
        if sides:
            if any(r == sides for r in highlight_rolls):
                rolls_text = f"[b green]{rolls}[/b green]"
            elif any(r == 1 for r in highlight_rolls):
                rolls_text = f"[b red]{rolls}[/b red]"

        text = f"{name} rolls {notation}{label}: {total} {rolls_text}"
        if payload.get("dc") is not None:
            text += f" vs DC {payload['dc']}"
            success = payload.get("success")
            if success is not None:
                text += " — success" if success else " — failure"
        return text

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
            elif session_screen is not None and kind == "dice":
                # Skipped here, not written - the structured dice_result
                # branch below renders the same roll with individual-die
                # highlighting the plain text can't carry. Both envelopes
                # broadcast for every roll (server/engine.py); rendering
                # both here would show each roll twice.
                pass
            elif session_screen is not None:
                session_screen.write_log(text)
            elif kind == "chat" and isinstance(self.screen, LobbyScreen):
                self.screen.query_one("#chat-log", RichLog).write(text)
            return

        if envelope.type == "dice_result":
            # Closes docs/protocol.md's own documented "known client gap" -
            # this used to have no handler at all, so a roll's only visible
            # trace was the plain log_entry text line skipped above. Built
            # from the structured payload instead so a natural max/min on
            # any individual die can actually be highlighted, not just
            # reproduced as the same flat text.
            if session_screen is not None:
                session_screen.write_log(self._dice_result_line(envelope.payload))
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
