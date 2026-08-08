from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from client.app import (
    CharacterSheetPanel,
    DungeonMasterApp,
    LobbyScreen,
    SessionScreen,
    WelcomeScreen,
    _npc_status_line,
)
from shared.protocol import Envelope
from textual.css.query import NoMatches


class FakeTransport:
    """Records every sent envelope type/payload instead of touching a real
    socket - these tests exercise the client's own screen/transition logic,
    not the transport layer (already covered for real by
    tests/test_transport_e2e.py's live websocket tests)."""

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        pass

    async def send(self, type_: str, payload: dict) -> None:
        self.sent.append((type_, payload))

    async def messages(self):
        return
        yield  # pragma: no cover - never actually iterated; _handle() is called directly instead


def _state_sync(player_id: str, *, started: bool, characters: dict | None = None) -> Envelope:
    return Envelope(
        type="state_sync", session_id="s", sender_id="server",
        payload={
            "characters": characters or {player_id: {"name": "Thrain", "hp": 10, "max_hp": 10}},
            "npcs": {},
            "world_state": {},
            "turn_order": [player_id],
            "current_turn": player_id,
            "log_tail": [],
            "started": started,
        },
    )


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


def _log_has_styled_segment(rich_log, color: str) -> bool:
    # RichLog is constructed with markup=True, so a written "[b green]..."
    # tag is parsed into a real styled Segment, not left as literal bracket
    # text - checking .text for the raw markup string would never match.
    return any(
        color in str(seg.style)
        for strip in rich_log.lines
        for seg in strip._segments
    )


async def test_welcome_screen_is_the_first_screen_and_prompts_for_class_when_new():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test():
            assert isinstance(app.screen, WelcomeScreen)
            assert app.screen.query_one("#class-input") is not None


async def test_returning_player_does_not_see_class_prompt():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=False)
        async with app.run_test():
            with pytest.raises(NoMatches):
                app.screen.query_one("#class-input")


async def test_welcome_screen_shows_import_field_when_new():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test():
            assert app.screen.query_one("#import-input") is not None


async def test_returning_player_does_not_see_import_field():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=False)
        async with app.run_test():
            with pytest.raises(NoMatches):
                app.screen.query_one("#import-input")


async def test_join_with_valid_import_file_sends_imported_character(tmp_path):
    character_data = {"name": "Torvin", "hp": 5, "max_hp": 12, "xp": 300, "level": 2}
    import_path = tmp_path / "torvin.json"
    import_path.write_text(json.dumps(character_data))

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#import-input")
            await pilot.press(*str(import_path))
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport.sent[-1][0] == "join_session"
            assert app.transport.sent[-1][1]["imported_character"] == character_data


async def test_join_with_unreadable_import_path_shows_error_and_does_not_connect(tmp_path):
    missing_path = tmp_path / "does-not-exist.json"

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#import-input")
            await pilot.press(*str(missing_path))
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport is None, "a bad import path should stop join_session from ever being sent"
            error = app.screen.query_one("#welcome-error")._Static__content
            assert "couldn't read" in str(error).lower()


async def test_join_with_invalid_json_import_shows_error(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json")

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#import-input")
            await pilot.press(*str(bad_path))
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport is None
            error = app.screen.query_one("#welcome-error")._Static__content
            assert "valid json" in str(error).lower()


async def test_join_with_blank_import_field_omits_imported_character():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport.sent[-1] == ("join_session", {"player_name": "Thrain", "character_class": ""})


async def test_fresh_join_lands_on_lobby_not_session():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await pilot.pause()

            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            assert isinstance(app.screen, LobbyScreen)
            assert app.transport.sent[-1] == ("join_session", {"player_name": "Thrain", "character_class": ""})


async def test_reconnect_into_started_session_skips_the_lobby():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=False)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()

            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            assert isinstance(app.screen, SessionScreen)


async def test_start_button_sends_start_session_and_disables_itself():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await pilot.click("#start")
            await pilot.pause()

            assert ("start_session", {}) in app.transport.sent
            assert app.screen.query_one("#start").disabled


async def test_session_started_transitions_lobby_to_session_screen():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()
            assert isinstance(app.screen, LobbyScreen)

            await app._handle(Envelope(type="session_started", session_id="s", sender_id="server", payload={}))
            await pilot.pause()

            assert isinstance(app.screen, SessionScreen)


async def test_narration_streams_into_session_screen_after_session_started():
    # This is the exact ordering the server relies on (session_started
    # broadcasts before the opening scene's narration - see
    # GameEngine._on_start_session): a client must already be on
    # SessionScreen, with a real #log widget, by the time narration
    # chunks arrive, or they'd have nowhere to render.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(type="session_started", session_id="s", sender_id="server", payload={}))
            await pilot.pause()

            # Matches the real server's actual streaming shape
            # (GameEngine._narrate_and_apply/_log_envelope): text arrives
            # via done=False chunks, then a final empty done=True flush -
            # not one message carrying the full text with done=True.
            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "narration", "text": "A cold wind blows.", "done": False},
            ))
            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "narration", "text": "", "done": True},
            ))
            await pilot.pause()

            assert "cold wind" in _log_text(app.screen.query_one("#log"))


async def test_lobby_chat_writes_to_chat_log_not_the_session_log():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "chat", "text": "Rowan: hello!"},
            ))
            await pilot.pause()

            assert "hello" in _log_text(app.screen.query_one("#chat-log"))


async def test_lobby_export_command_writes_character_file_not_chat(tmp_path):
    export_path = tmp_path / "mychar"
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync(
                "p1", started=False,
                characters={"p1": {
                    "player_id": "p1", "name": "Thrain", "hp": 10, "max_hp": 10, "xp": 50, "level": 1,
                }},
            ))
            await pilot.pause()

            await pilot.click("#chat-input")
            await pilot.press(*f"/export {export_path}", "enter")
            await pilot.pause()

            written = json.loads((tmp_path / "mychar.json").read_text())
            assert written["name"] == "Thrain"
            assert written["xp"] == 50
            assert "exported" in _log_text(app.screen.query_one("#chat-log")).lower()
            assert app.transport.sent[-1][0] != "chat_message", "/export must not also be sent as chat"


async def test_session_export_command_writes_character_file(tmp_path):
    export_path = tmp_path / "session_char"
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*f"/export {export_path}", "enter")
            await pilot.pause()

            written = json.loads((tmp_path / "session_char.json").read_text())
            assert written["name"] == "Thrain"
            assert "exported" in _log_text(app.screen.query_one("#log")).lower()


async def test_export_command_with_no_filename_uses_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/export", "enter")
            await pilot.pause()

            assert (tmp_path / "character.json").exists()


async def test_note_command_sends_character_edit_not_chat():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/note the old man owes me a favor", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == (
                "character_edit", {"field": "notes", "value": "the old man owes me a favor"}
            )


async def test_item_add_command_sends_character_edit():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/item add a shiny rock", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("character_edit", {"field": "add_item", "value": "a shiny rock"})


async def test_item_remove_command_sends_character_edit():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/item remove a torch", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("character_edit", {"field": "remove_item", "value": "a torch"})


async def test_advisory_system_message_renders_with_distinct_yellow_styling():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="system_message", session_id="s", sender_id="server",
                payload={"level": "warning", "text": "your sheet might be out of sync", "advisory": True},
            ))
            await pilot.pause()

            log = app.screen.query_one("#log")
            assert "out of sync" in _log_text(log)
            assert _log_has_styled_segment(log, "yellow")


async def test_ordinary_system_message_does_not_get_advisory_styling():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="system_message", session_id="s", sender_id="server",
                payload={"level": "warning", "text": "It's not your turn."},
            ))
            await pilot.pause()

            log = app.screen.query_one("#log")
            assert "not your turn" in _log_text(log)
            assert not _log_has_styled_segment(log, "yellow")


def test_npc_status_line_omits_neutral_disposition():
    # neutral is the field's own default and the common case - only a real,
    # DM-set non-neutral disposition is worth a reader's attention.
    line = _npc_status_line("goblin", {"hp": 3, "max_hp": 7, "disposition": "neutral"})
    assert "neutral" not in line


def test_npc_status_line_shows_a_non_neutral_disposition():
    line = _npc_status_line("goblin", {"hp": 3, "max_hp": 7, "disposition": "hostile"})
    assert "hostile" in line


def test_npc_status_line_omits_disposition_entirely_when_absent():
    # A legacy/pre-disposition NPC dict (no key at all, not just "neutral")
    # should render exactly as it always has, no crash on a missing key.
    line = _npc_status_line("goblin", {"hp": 3, "max_hp": 7})
    assert "hostile" not in line and "friendly" not in line and "neutral" not in line


async def test_npc_update_with_disposition_renders_in_the_log():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="npc_update", session_id="s", sender_id="server",
                payload={"name": "goblin", "sheet_delta": {"hp": 3, "max_hp": 7, "disposition": "hostile"}},
            ))
            await pilot.pause()

            assert "hostile" in _log_text(app.screen.query_one("#log"))


async def test_deathsave_command_sends_death_save_with_no_payload():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/deathsave", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("death_save", {})


def test_death_status_label_is_empty_for_a_healthy_character():
    assert CharacterSheetPanel._death_status_label(10) == ""


def test_death_status_label_shows_stable_at_zero_hp_with_no_flags():
    assert "STABLE" in CharacterSheetPanel._death_status_label(0)


def test_death_status_label_shows_dying_over_stable():
    label = CharacterSheetPanel._death_status_label(0, dying=True)
    assert "DYING" in label
    assert "STABLE" not in label


def test_death_status_label_shows_dead_over_dying():
    label = CharacterSheetPanel._death_status_label(0, dying=True, dead=True)
    assert "DEAD" in label
    assert "DYING" not in label


async def test_transcript_command_writes_plain_text_log_not_sent_to_server(tmp_path):
    transcript_path = tmp_path / "my_session"
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            # Simulates the real server's own chat_message -> broadcast
            # log_entry round trip (FakeTransport doesn't echo anything
            # back on its own) so there's real content in #log to export.
            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "chat", "text": "hello party"},
            ))
            await pilot.pause()

            sent_before = len(app.transport.sent)
            await pilot.click("#input")
            await pilot.press(*f"/transcript {transcript_path}", "enter")
            await pilot.pause()

            # A pure client-side read of the already-rendered log - nothing
            # about it should reach the server.
            assert len(app.transport.sent) == sent_before

            written = (tmp_path / "my_session.txt").read_text()
            assert "hello party" in written
            assert "Transcript saved" in _log_text(app.screen.query_one("#log"))


async def test_transcript_command_with_no_filename_uses_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/transcript", "enter")
            await pilot.pause()

            assert (tmp_path / "transcript.txt").exists()


async def test_party_updates_render_in_lobby_sheet_panel_without_inventory():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(
                type="player_joined", session_id="s", sender_id="server",
                payload={
                    "player_id": "p2", "name": "Rowan", "character_class": "Rogue",
                    "hp": 8, "max_hp": 8, "conditions": [],
                },
            ))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet")
            assert "p2" in sheet._others
            rendered = sheet._Static__content
            assert "Rowan" in rendered
            assert "inventory" not in rendered.lower()


async def test_sheet_panel_renders_own_ability_scores_with_modifiers():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {
                    "player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12,
                    "stats": {"str": 15, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
                    "stat_modifiers": {"str": 2, "dex": 1, "con": 2, "int": -1, "wis": 1, "cha": 0},
                },
            }))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet")._Static__content
            assert "Ability Scores" in rendered
            assert "STR 15 (+2)" in rendered
            assert "INT 8 (-1)" in rendered
            assert "CHA 10 (+0)" in rendered


async def test_party_view_never_shows_ability_scores():
    # stats/stat_modifiers are owner-only (server/engine.py's
    # _public_character_view deliberately excludes them) - a party
    # member's public view dict simply never carries the key, but this
    # locks the client's own rendering side of that boundary too.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(
                type="player_joined", session_id="s", sender_id="server",
                payload={
                    "player_id": "p2", "name": "Rowan", "character_class": "Rogue",
                    "hp": 8, "max_hp": 8, "conditions": [], "level": 1,
                },
            ))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet")._Static__content
            assert "Ability Scores" not in rendered


async def test_sheet_panel_renders_own_ac_next_to_hp():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12, "ac": 15},
            }))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet")._Static__content
            assert "AC 15" in rendered


async def test_party_view_shows_ac_since_it_is_public():
    # Unlike ability scores/xp, ac is part of the public view
    # (server/engine.py's _public_character_view) - a real fact about
    # combat capability visible to the party, the same as HP or level.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(
                type="player_joined", session_id="s", sender_id="server",
                payload={
                    "player_id": "p2", "name": "Rowan", "character_class": "Rogue",
                    "hp": 8, "max_hp": 8, "ac": 13, "conditions": [], "level": 1,
                },
            ))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet")._Static__content
            assert "AC 13" in rendered


async def test_turn_prompt_for_another_player_names_them_not_just_your_own_turn():
    # A real gap found only by running two real clients through a real
    # session (ROADMAP.md): turn_prompt used to only ever write something
    # for the player whose turn it now is - anyone else got no indication
    # at all of whose turn it was. self.others (already populated via
    # player_joined/state_sync) is what makes naming the other player
    # possible without a new protocol field.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="player_joined", session_id="s", sender_id="server",
                payload={
                    "player_id": "p2", "name": "Rowan", "character_class": "Rogue",
                    "hp": 8, "max_hp": 8, "conditions": [],
                },
            ))
            await app._handle(Envelope(
                type="turn_prompt", session_id="s", sender_id="server",
                payload={"player_id": "p2", "prompt_text": "What do you do?"},
            ))
            await pilot.pause()

            log_text = "\n".join(strip.text for strip in app.screen.query_one("#log").lines)
            assert "Rowan's turn" in log_text
            assert "Your turn" not in log_text

            await app._handle(Envelope(
                type="turn_prompt", session_id="s", sender_id="server",
                payload={"player_id": "p1", "prompt_text": "What do you do?"},
            ))
            await pilot.pause()

            log_text = "\n".join(strip.text for strip in app.screen.query_one("#log").lines)
            assert "Your turn" in log_text


async def test_dice_result_renders_with_natural_max_highlighted():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Thrain", "hp": 10, "max_hp": 10},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d20", "result": 20, "rolls": [20],
                    "sides": 20, "purpose": "attack",
                },
            ))
            await pilot.pause()

            log = app.screen.query_one("#log")
            assert "Thrain rolls 1d20 (attack): 20" in _log_text(log)
            assert _log_has_styled_segment(log, "green")


async def test_dice_result_does_not_duplicate_the_plain_log_entry_line():
    # Both envelopes broadcast for every real roll (server/engine.py) -
    # the client must render exactly one line per roll, not two.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "dice", "text": "Thrain rolls 1d20: 13 [13]"},
            ))
            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={"roller_id": "p1", "dice": "1d20", "result": 13, "rolls": [13], "sides": 20, "purpose": ""},
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert log_text.count("rolls 1d20") == 1


async def test_dice_result_shows_ability_modifier_tag_when_present():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Thrain", "hp": 10, "max_hp": 10},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d20", "result": 15, "rolls": [14],
                    "sides": 20, "purpose": "dexterity check", "dc": 12, "success": True,
                    "ability": "dex", "ability_modifier": 1,
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "1d20 +1 DEX" in log_text
            assert "15" in log_text


async def test_dice_result_shows_damage_type_and_ability_together():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d8", "result": 7, "rolls": [5],
                    "sides": 8, "purpose": "damage roll",
                    "ability": "str", "ability_modifier": 2, "damage_type": "slashing",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "1d8 (slashing) +2 STR" in log_text


async def test_dice_result_shows_disadvantage_tag_and_kept_roll():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d20", "result": 4, "rolls": [18, 4],
                    "sides": 20, "purpose": "stealth check",
                    "disadvantage": True, "disadvantage_reasons": ["poisoned"],
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "disadvantage: poisoned" in log_text
            assert "18, 4" in log_text  # both real rolls still shown


async def test_dice_result_disadvantage_highlight_uses_kept_roll_not_either_raw_die():
    # A real, subtle bug this test locks against: with disadvantage,
    # `rolls` holds both d20s ([18, 4] here) - naively checking "does
    # *any* entry equal `sides`" would wrongly highlight this roll green
    # off the discarded 18, even though the kept (and displayed) result is
    # a low, unremarkable 4.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d20", "result": 4, "rolls": [20, 4],
                    "sides": 20, "purpose": "stealth check",
                    "disadvantage": True, "disadvantage_reasons": ["poisoned"],
                },
            ))
            await pilot.pause()

            log = app.screen.query_one("#log")
            assert not _log_has_styled_segment(log, "green")


async def test_dice_result_names_the_other_players_roll():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()
            await app._handle(Envelope(
                type="player_joined", session_id="s", sender_id="server",
                payload={
                    "player_id": "p2", "name": "Rowan", "character_class": "Rogue",
                    "hp": 8, "max_hp": 8, "conditions": [],
                },
            ))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={"roller_id": "p2", "dice": "1d20", "result": 1, "rolls": [1], "sides": 20, "purpose": ""},
            ))
            await pilot.pause()

            log = app.screen.query_one("#log")
            assert "Rowan rolls 1d20: 1" in _log_text(log)
            assert _log_has_styled_segment(log, "red")
