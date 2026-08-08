from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static

from shared.protocol import Envelope

from .transport import ClientTransport


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
        lines = [
            name_line,
            f"HP: {character.get('hp')}/{character.get('max_hp')}",
        ]
        stats = character.get("stats") or {}
        if stats:
            lines.append("")
            lines.extend(f"{k}: {v}" for k, v in stats.items())
        inventory = character.get("inventory") or []
        if inventory:
            lines.append("")
            lines.append("[b]Inventory[/b]")
            lines.extend(f"- {item}" for item in inventory)
        conditions = character.get("conditions") or []
        if conditions:
            lines.append("")
            lines.append("[b]Conditions[/b]")
            lines.extend(f"- {c}" for c in conditions)

        active_objectives = [o for o in (self._world.get("objectives") or []) if o.get("status") == "active"]
        if active_objectives:
            lines.append("")
            lines.append("[b]Objectives[/b]")
            lines.extend(f"- {o['text']}" for o in active_objectives)

        if self._others:
            lines.append("")
            lines.append("[b]Other Players[/b]")
            lines.extend(self._other_player_line(other) for other in self._others.values())

        self.update("\n".join(lines))

    @staticmethod
    def _other_player_line(other: dict) -> str:
        line = f"- {other.get('name', '?')}"
        character_class = other.get("character_class")
        if character_class:
            line += f" ({character_class})"
        line += f": HP {other.get('hp')}/{other.get('max_hp')}"
        conditions = other.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line


class DungeonMasterApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    CharacterSheetPanel { width: 30%; border: solid $accent; padding: 1; }
    RichLog { width: 70%; border: solid $accent; }
    #status { height: 1; padding: 0 1; }
    """

    def __init__(self, uri: str, session_id: str, player_id: str, player_name: str, character_class: str = ""):
        super().__init__()
        self._transport = ClientTransport(uri, session_id, player_id)
        self._player_id = player_id
        self._player_name = player_name
        self._character_class = character_class
        self._narration_buffer = ""
        self._others: dict[str, dict] = {}  # player_id -> public view, keyed the same way the server sends it

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield CharacterSheetPanel(id="sheet")
            yield RichLog(id="log", wrap=True, markup=True)
        yield Static("", id="status")
        yield Input(placeholder="What do you do? (/roll 1d20, /chat hello)", id="input")
        yield Footer()

    def _set_thinking(self, thinking: bool) -> None:
        self.query_one("#status", Static).update("[dim]The DM is thinking...[/dim]" if thinking else "")

    async def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        await self._transport.connect()
        await self._transport.send(
            "join_session", {"player_name": self._player_name, "character_class": self._character_class}
        )
        asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        async for envelope in self._transport.messages():
            self._handle(envelope)

    def _handle(self, envelope: Envelope) -> None:
        log = self.query_one("#log", RichLog)
        sheet = self.query_one("#sheet", CharacterSheetPanel)

        if envelope.type == "state_sync":
            characters = envelope.payload.get("characters", {})
            mine = characters.get(self._player_id)
            if mine:
                sheet.render_sheet(mine)
            self._others = {pid: c for pid, c in characters.items() if pid != self._player_id}
            sheet.render_others(self._others)
            sheet.render_world(envelope.payload.get("world_state", {}))
            for name, npc in envelope.payload.get("npcs", {}).items():
                log.write(self._npc_status_line(name, npc))
            for entry in envelope.payload.get("log_tail", []):
                log.write(entry.get("text", ""))

        elif envelope.type == "character_update":
            if envelope.payload.get("player_id") == self._player_id:
                sheet.render_sheet(envelope.payload.get("sheet_delta", {}))

        elif envelope.type in ("player_joined", "player_update"):
            pid = envelope.payload.get("player_id")
            if pid and pid != self._player_id:
                self._others[pid] = envelope.payload
                sheet.render_others(self._others)

        elif envelope.type == "player_left":
            pid = envelope.payload.get("player_id")
            if self._others.pop(pid, None) is not None:
                sheet.render_others(self._others)

        elif envelope.type == "npc_update":
            name = envelope.payload.get("name", "?")
            log.write(self._npc_status_line(name, envelope.payload.get("sheet_delta", {})))

        elif envelope.type == "world_update":
            sheet.render_world(envelope.payload)

        elif envelope.type == "log_entry":
            text = envelope.payload.get("text", "")
            if envelope.payload.get("kind") == "narration":
                self._set_thinking(False)  # the silent gap this fills ends the moment real text starts arriving
                if envelope.payload.get("done"):
                    log.write(self._narration_buffer)
                    self._narration_buffer = ""
                else:
                    self._narration_buffer += text
            else:
                log.write(text)

        elif envelope.type == "turn_prompt":
            if envelope.payload.get("player_id") == self._player_id:
                log.write("[i]Your turn.[/i]")

        elif envelope.type == "system_message":
            self._set_thinking(False)  # covers the narration-failed path, which never reaches a log_entry at all
            log.write(f"[dim]{envelope.payload.get('text', '')}[/dim]")

    @staticmethod
    def _npc_status_line(name: str, npc: dict) -> str:
        line = f"[dim]{name}: HP {npc.get('hp')}/{npc.get('max_hp')}"
        conditions = npc.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line + "[/dim]"

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/roll "):
            dice, _, reason = text[len("/roll "):].strip().partition(" ")
            await self._transport.send("dice_roll", {"dice": dice, "reason": reason})
        elif text.startswith("/chat "):
            await self._transport.send("chat_message", {"text": text[len("/chat "):].strip()})
        else:
            # Only a real player_action triggers a DM narrate() call - /roll
            # and /chat are handled instantly server-side with no LLM in the
            # loop, so there's nothing to wait on for those.
            self._set_thinking(True)
            await self._transport.send("player_action", {"text": text})
