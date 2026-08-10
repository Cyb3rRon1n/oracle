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
        # ClientTransport(uri, session_id, player_id) - captured positionally
        # since that's the real signature (client/transport.py), needed by
        # WelcomeScreen's Solo-mode tests to confirm what session_id it
        # actually chose without reaching into a real websocket.
        self.session_id = args[1] if len(args) > 1 else kwargs.get("session_id")

    async def connect(self) -> None:
        pass

    async def send(self, type_: str, payload: dict) -> None:
        self.sent.append((type_, payload))

    async def messages(self):
        return
        yield  # pragma: no cover - never actually iterated; _handle() is called directly instead


def _state_sync(
    player_id: str, *, started: bool, characters: dict | None = None, world_state: dict | None = None
) -> Envelope:
    return Envelope(
        type="state_sync", session_id="s", sender_id="server",
        payload={
            "characters": characters or {player_id: {"name": "Thrain", "hp": 10, "max_hp": 10}},
            "npcs": {},
            "world_state": world_state or {},
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


async def test_welcome_screen_defaults_to_solo_with_session_field_hidden():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test():
            assert app.screen.query_one("#mode-solo").value is True
            assert app.screen.query_one("#session-input").display is False


async def test_selecting_multiplayer_reveals_the_session_field():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#mode-multiplayer")
            await pilot.pause()
            assert app.screen.query_one("#session-input").display is True


async def test_solo_join_uses_a_session_id_derived_from_player_id():
    # Not "default", not blank, and never a fresh random id each time
    # (that would prevent ever resuming a solo game) - stable and
    # collision-free simply by being derived from this client's own
    # already-unique player_id. See client/app.py's WelcomeScreen._join.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="my-stable-id", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport.session_id == "solo-my-stable-id"
            assert app.transport.sent[-1][0] == "join_session"


async def test_solo_join_session_id_is_stable_across_separate_clients_with_the_same_player_id():
    for _ in range(2):
        with patch("client.app.ClientTransport", FakeTransport):
            app = DungeonMasterApp(uri="ws://x", player_id="same-id", is_new_character=True)
            async with app.run_test() as pilot:
                await pilot.click("#join")
                await pilot.pause()
                assert app.transport.session_id == "solo-same-id"


async def test_multiplayer_join_with_typed_session_id_uses_it_exactly():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        # Taller than the 80x24 default - a new character's welcome box
        # (name/mode-select/session/class/import/join/error) genuinely
        # doesn't fit the default viewport once session-input is revealed,
        # and pilot.click() needs a target actually on-screen, not just
        # present in the scrollable container.
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.click("#mode-multiplayer")
            await pilot.pause()  # let the newly-revealed session-input finish mounting before clicking it
            await pilot.click("#session-input")
            await pilot.press(*"friends-game")
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport.session_id == "friends-game"


async def test_multiplayer_join_with_blank_session_id_falls_back_to_default():
    # Preserves the exact pre-existing behavior for anyone who picks
    # Multiplayer but leaves the field blank - unchanged from before Solo
    # mode existed.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.click("#mode-multiplayer")
            await pilot.pause()  # let the newly-revealed session-input finish mounting first
            await pilot.click("#join")
            await pilot.pause()

            assert app.transport.session_id == "default"


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


async def test_equip_command_sends_character_edit():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/equip Shortbow", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("character_edit", {"field": "equip", "value": "Shortbow"})


async def test_unequip_command_sends_character_edit():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/unequip Longsword", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("character_edit", {"field": "unequip", "value": "Longsword"})


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


def test_npc_line_shows_defeated_at_zero_hp():
    line = CharacterSheetPanel._npc_line("goblin", {"hp": 0, "max_hp": 7})
    assert "(defeated)" in line


def test_npc_line_omits_defeated_label_above_zero_hp():
    line = CharacterSheetPanel._npc_line("goblin", {"hp": 3, "max_hp": 7})
    assert "defeated" not in line


def test_npc_line_shows_disposition_and_conditions():
    line = CharacterSheetPanel._npc_line(
        "goblin", {"hp": 3, "max_hp": 7, "disposition": "hostile", "conditions": ["poisoned"]}
    )
    assert "hostile" in line
    assert "poisoned" in line


async def test_state_sync_npcs_render_in_the_persistent_panel():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            # _state_sync's helper defaults npcs={} - build the payload
            # directly to cover a (re)joining player seeing existing NPC
            # state immediately, not just a live npc_update after joining.
            await app._handle(Envelope(
                type="state_sync", session_id="s", sender_id="server",
                payload={
                    "characters": {"p1": {"name": "Thrain", "hp": 10, "max_hp": 10}},
                    "npcs": {"goblin": {"hp": 3, "max_hp": 7}},
                    "world_state": {}, "turn_order": ["p1"], "current_turn": "p1",
                    "log_tail": [], "started": True,
                },
            ))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "NPCs" in rendered
            assert "goblin" in rendered


async def test_npc_update_refreshes_the_persistent_panel_not_just_the_log():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="npc_update", session_id="s", sender_id="server",
                payload={"name": "goblin", "sheet_delta": {"hp": 3, "max_hp": 7}},
            ))
            await pilot.pause()
            assert "goblin" in app.screen.query_one("#sheet", CharacterSheetPanel).all_text()

            # A second update for the same NPC must replace, not duplicate,
            # its panel entry - the exact staleness bug this feature fixes
            # (self.npcs was previously only ever set once, from
            # state_sync, and never updated by a live npc_update at all).
            await app._handle(Envelope(
                type="npc_update", session_id="s", sender_id="server",
                payload={"name": "goblin", "sheet_delta": {"hp": 0, "max_hp": 7}},
            ))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert rendered.count("goblin") == 1
            assert "(defeated)" in rendered


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


async def test_combat_start_command_sends_start_combat_with_no_payload():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/combat start", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("start_combat", {})


async def test_combat_end_command_sends_end_combat_with_no_payload():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await pilot.click("#input")
            await pilot.press(*"/combat end", "enter")
            await pilot.pause()

            assert app.transport.sent[-1] == ("end_combat", {})


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


async def test_lobby_transcript_command_writes_chat_log_not_session_log(tmp_path):
    transcript_path = tmp_path / "lobby_chat"
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "chat", "text": "Rowan: see you all soon"},
            ))
            await pilot.pause()

            sent_before = len(app.transport.sent)
            await pilot.click("#chat-input")
            await pilot.press(*f"/transcript {transcript_path}", "enter")
            await pilot.pause()

            # A pure client-side read - /transcript must never reach the
            # server, the same as SessionScreen's own version.
            assert len(app.transport.sent) == sent_before

            written = (tmp_path / "lobby_chat.txt").read_text()
            assert "see you all soon" in written
            assert "Transcript saved" in _log_text(app.screen.query_one("#chat-log"))


async def test_lobby_transcript_command_with_no_filename_uses_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            await pilot.click("#chat-input")
            await pilot.press(*"/transcript", "enter")
            await pilot.pause()

            assert (tmp_path / "lobby-chat.txt").exists()


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
            rendered = sheet.all_text()
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

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
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

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "Ability Scores" not in rendered


async def test_sheet_panel_renders_known_spells_and_slots_for_a_caster():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {
                    "player_id": "p1", "name": "Gandalf", "hp": 6, "max_hp": 6,
                    "known_spells": ["fire_bolt", "magic_missile"],
                    "spell_slots": {"1": 1}, "max_spell_slots": {"1": 2},
                    "spell_save_dc": 13,
                },
            }))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "Spells" in rendered
            assert "DC 13" in rendered
            assert "Slots: 1 1/2" in rendered
            assert "Fire Bolt" in rendered
            assert "Magic Missile" in rendered


async def test_sheet_panel_shows_no_spells_section_for_a_non_caster():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "Spells" not in rendered


async def test_party_view_never_shows_spells_since_they_are_private():
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
                    "player_id": "p2", "name": "Gandalf", "character_class": "Wizard",
                    "hp": 6, "max_hp": 6, "conditions": [], "level": 1,
                },
            ))
            await pilot.pause()

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "Spells" not in rendered


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

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
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

            rendered = app.screen.query_one("#sheet", CharacterSheetPanel).all_text()
            assert "AC 13" in rendered


async def test_sheet_panel_renders_class_features_and_skill_proficiencies():
    # class_features/skill_proficiencies are new owner-only fields
    # (server/engine.py's _owner_character_view, ROADMAP.md item 7) - real
    # SRD/CLASS_SKILL_PROFICIENCIES data that previously had nowhere to
    # render at all.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {
                    "player_id": "p1", "name": "Elowen", "hp": 6, "max_hp": 6,
                    "class_features": ["Arcane Recovery: recover expended spell slots."],
                    "skill_proficiencies": ["arcana", "investigation"],
                },
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            assert "Arcane Recovery" in sheet._features_text()
            assert "Arcana" in sheet._abilities_text()
            assert "Investigation" in sheet._abilities_text()


async def test_sheet_panel_renders_notes_on_the_features_tab():
    # notes is set via /note (character_edit) but was never rendered
    # anywhere on the sheet before the Features & Notes tab existed - a
    # real pre-existing gap, not new functionality.
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12, "notes": "Owes the smith a favor."},
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            assert "Owes the smith a favor." in sheet._features_text()


async def test_inventory_tab_separates_equipped_from_carried():
    # A direct owner ask - not a flat item list, an Equipped section
    # (weapon/armor slots) distinct from Carried (everything else).
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {
                    "player_id": "p1", "name": "Rook", "hp": 12, "max_hp": 12,
                    "inventory": ["Longsword", "Leather Armor", "a torch", "3 rations"],
                    "equipped_weapon": "Longsword", "equipped_armor": "Leather Armor",
                },
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            rendered = sheet._inventory_text()
            assert "Weapon: Longsword" in rendered
            assert "Armor: Leather Armor" in rendered
            assert "a torch" in rendered
            assert "3 rations" in rendered
            # Equipped items shouldn't also appear a second time under Carried.
            assert rendered.count("Longsword") == 1
            assert rendered.count("Leather Armor") == 1


async def test_inventory_tab_shows_none_for_empty_equipment_slots():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Rook", "hp": 12, "max_hp": 12, "inventory": ["a torch"]},
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            rendered = sheet._inventory_text()
            assert "Weapon: " in rendered and "(none)" in rendered
            assert "Armor: " in rendered and "(none)" in rendered
            assert "a torch" in rendered


async def test_spells_tab_is_hidden_for_a_non_caster_and_shown_for_a_caster():
    from textual.widgets import TabbedContent

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            tabs = sheet.query_one("#sheet-tabs", TabbedContent)
            assert tabs.get_tab("tab-spells").has_class("-hidden")

            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {
                    "player_id": "p1", "name": "Elowen", "hp": 6, "max_hp": 6,
                    "known_spells": ["fire_bolt"], "spell_slots": {}, "max_spell_slots": {},
                },
            }))
            await pilot.pause()

            assert not tabs.get_tab("tab-spells").has_class("-hidden")


async def test_pagedown_and_pageup_cycle_the_active_sheet_tab():
    from textual.widgets import TabbedContent

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            tabs = sheet.query_one("#sheet-tabs", TabbedContent)
            assert tabs.active == "tab-overview"

            sheet.action_next_tab()
            assert tabs.active == "tab-abilities"

            sheet.action_previous_tab()
            assert tabs.active == "tab-overview"


async def test_next_tab_skips_the_hidden_spells_tab_for_a_non_caster():
    from textual.widgets import TabbedContent

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False, characters={
                "p1": {"player_id": "p1", "name": "Thrain", "hp": 12, "max_hp": 12},
            }))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            tabs = sheet.query_one("#sheet-tabs", TabbedContent)

            sheet.action_next_tab()  # overview -> abilities (map hidden, skipped - no location_map)
            sheet.action_next_tab()  # abilities -> inventory
            sheet.action_next_tab()  # inventory -> features (spells hidden, skipped)
            assert tabs.active == "tab-features"


async def test_map_tab_is_hidden_with_no_locations_and_shown_once_the_dm_registers_one():
    from textual.widgets import TabbedContent

    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=False))
            await pilot.pause()

            sheet = app.screen.query_one("#sheet", CharacterSheetPanel)
            tabs = sheet.query_one("#sheet-tabs", TabbedContent)
            assert tabs.get_tab("tab-map").has_class("-hidden")

            await app._handle(_state_sync(
                "p1", started=False,
                world_state={"location": "Great Hall", "location_map": {"Great Hall": ["Armory"], "Armory": ["Great Hall"]}},
            ))
            await pilot.pause()

            assert not tabs.get_tab("tab-map").has_class("-hidden")
            rendered = sheet._map_text()
            assert "Great Hall" in rendered
            assert "(here)" in rendered
            assert "-> Armory" in rendered


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


async def test_dice_result_shows_critical_hit_callout_when_present():
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
                    "sides": 20, "purpose": "sword swing", "dc": 15, "success": True,
                    "roll_kind": "attack", "critical": True,
                },
            ))
            await pilot.pause()

            # Two separate substring checks, not one "CRITICAL HIT!" check -
            # RichLog wraps long lines at the terminal width, which can
            # split the phrase across two Strips (and therefore two "\n"-
            # joined pieces of _log_text's output, with the wrap eating the
            # space between them) depending on exactly how much text
            # preceded it on the line.
            log_text = _log_text(app.screen.query_one("#log"))
            assert "CRITICAL" in log_text
            assert "HIT!" in log_text


async def test_dice_result_no_critical_tag_when_not_a_critical():
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
                    "roller_id": "p1", "dice": "1d20", "result": 12, "rolls": [12],
                    "sides": 20, "purpose": "sword swing", "dc": 15, "success": False,
                    "roll_kind": "attack",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "CRITICAL HIT!" not in log_text


async def test_outcome_log_entry_is_colored_by_category():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "outcome", "text": "Thrain: HP -5 (now 5/10)", "category": "damage"},
            ))
            await pilot.pause()

            assert _log_has_styled_segment(app.screen.query_one("#log"), "red")
            log_text = _log_text(app.screen.query_one("#log"))
            assert "HP -5" in log_text


async def test_outcome_log_entry_with_unrecognized_category_still_shows_uncolored():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True))
            await pilot.pause()

            await app._handle(Envelope(
                type="log_entry", session_id="s", sender_id="server",
                payload={"kind": "outcome", "text": "Thrain: something happened"},
            ))
            await pilot.pause()

            assert "something happened" in _log_text(app.screen.query_one("#log"))


async def test_dice_result_shows_roll_kind_tag_when_present():
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
                    "roller_id": "p1", "dice": "1d20", "result": 15, "rolls": [15],
                    "sides": 20, "purpose": "resist the poison", "dc": 12, "success": True,
                    "roll_kind": "save",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "1d20 (save)" in log_text


async def test_dice_result_shows_skill_and_proficiency_tag_when_present():
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
                    "roller_id": "p1", "dice": "1d20", "result": 19, "rolls": [15],
                    "sides": 20, "purpose": "climb the wall", "dc": 15, "success": True,
                    "ability": "str", "ability_modifier": 2,
                    "skill": "athletics", "proficient": True, "proficiency_bonus": 2,
                    "roll_kind": "check",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "Athletics, +2 proficiency" in log_text


async def test_dice_result_shows_spell_and_proficiency_tag_when_present():
    with patch("client.app.ClientTransport", FakeTransport):
        app = DungeonMasterApp(uri="ws://x", player_id="p1", is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#join")
            await pilot.pause()
            await app._handle(_state_sync("p1", started=True, characters={
                "p1": {"name": "Gandalf", "hp": 6, "max_hp": 6},
            }))
            await pilot.pause()

            await app._handle(Envelope(
                type="dice_result", session_id="s", sender_id="server",
                payload={
                    "roller_id": "p1", "dice": "1d10", "result": 12, "rolls": [10],
                    "sides": 10, "purpose": "", "dc": 12, "success": True,
                    "ability": "int", "ability_modifier": 3,
                    "damage_type": "fire", "spell": "Fire Bolt", "proficiency_bonus": 2,
                    "roll_kind": "attack",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "Fire Bolt, +2 proficiency" in log_text


async def test_dice_result_shows_skill_without_proficiency_tag_when_not_proficient():
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
                    "roller_id": "p1", "dice": "1d20", "result": 13, "rolls": [15],
                    "sides": 20, "purpose": "sneak past", "dc": 15, "success": False,
                    "ability": "dex", "ability_modifier": -2,
                    "skill": "stealth", "proficient": False,
                    "roll_kind": "check",
                },
            ))
            await pilot.pause()

            log_text = _log_text(app.screen.query_one("#log"))
            assert "Stealth" in log_text
            assert "proficiency" not in log_text


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
