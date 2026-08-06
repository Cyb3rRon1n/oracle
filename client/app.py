from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static

from shared.protocol import Envelope

from .transport import ClientTransport


class CharacterSheetPanel(Static):
    def render_sheet(self, character: dict) -> None:
        lines = [
            f"[b]{character.get('name', '?')}[/b]",
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
        self.update("\n".join(lines))


class DungeonMasterApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    CharacterSheetPanel { width: 30%; border: solid $accent; padding: 1; }
    RichLog { width: 70%; border: solid $accent; }
    """

    def __init__(self, uri: str, session_id: str, player_id: str, player_name: str):
        super().__init__()
        self._transport = ClientTransport(uri, session_id, player_id)
        self._player_id = player_id
        self._player_name = player_name
        self._narration_buffer = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield CharacterSheetPanel(id="sheet")
            yield RichLog(id="log", wrap=True, markup=True)
        yield Input(placeholder="What do you do? (/roll 1d20, /chat hello)", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        await self._transport.connect()
        await self._transport.send("join_session", {"player_name": self._player_name})
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
            for name, npc in envelope.payload.get("npcs", {}).items():
                log.write(self._npc_status_line(name, npc))
            for entry in envelope.payload.get("log_tail", []):
                log.write(entry.get("text", ""))

        elif envelope.type == "character_update":
            if envelope.payload.get("player_id") == self._player_id:
                sheet.render_sheet(envelope.payload.get("sheet_delta", {}))

        elif envelope.type == "npc_update":
            name = envelope.payload.get("name", "?")
            log.write(self._npc_status_line(name, envelope.payload.get("sheet_delta", {})))

        elif envelope.type == "log_entry":
            text = envelope.payload.get("text", "")
            if envelope.payload.get("kind") == "narration":
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
            await self._transport.send("player_action", {"text": text})
