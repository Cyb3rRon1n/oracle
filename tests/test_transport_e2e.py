from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect

from client.app import DungeonMasterApp, LobbyScreen, SessionScreen, WelcomeScreen
from server.engine import GameEngine
from server.state import Session
from server.transport import Transport
from shared.protocol import Envelope


class StubDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "ok."


async def test_join_over_real_websocket():
    session = Session(session_id="e2e-session")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, StubDM(), broadcast, send_to)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8799))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        ws = await connect("ws://localhost:8799")
        try:
            join = Envelope(
                type="join_session", session_id="e2e-session", sender_id=player_id,
                payload={"player_name": "Rook"},
            )
            await ws.send(join.to_json())

            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            env = Envelope.from_json(raw)
            assert env.type == "state_sync"
            assert env.payload["characters"][player_id]["name"] == "Rook"
        finally:
            await ws.close()
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def _recv_until(ws, event_type: str, timeout: float = 5) -> Envelope:
    """Drains messages off a real connection until one of the given type
    shows up - join/presence broadcasts interleave with other traffic
    (system_message, turn_prompt, ...), so a plain single recv() isn't
    reliable here."""
    async with asyncio.timeout(timeout):
        while True:
            raw = await ws.recv()
            env = Envelope.from_json(raw)
            if env.type == event_type:
                return env


async def test_second_player_sees_first_redacted_then_first_players_disconnect_broadcasts_player_left():
    session = Session(session_id="e2e-session-2")

    def engine_factory(broadcast, send_to):
        # enable_opening_scene=False: keeps this test's message ordering
        # predictable - a real opening scene would interleave several extra
        # log_entry broadcasts between join and turn_prompt.
        return GameEngine(session, StubDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8800))
    await asyncio.sleep(0.3)  # let the server bind

    player1_id = str(uuid.uuid4())
    player2_id = str(uuid.uuid4())
    try:
        ws1 = await connect("ws://localhost:8800")
        ws2 = await connect("ws://localhost:8800")
        try:
            await ws1.send(Envelope(
                type="join_session", session_id="e2e-session-2", sender_id=player1_id,
                payload={"player_name": "Rook", "character_class": "fighter"},
            ).to_json())
            await _recv_until(ws1, "player_joined")  # Rook's own join broadcast, back to itself

            await ws2.send(Envelope(
                type="join_session", session_id="e2e-session-2", sender_id=player2_id,
                payload={"player_name": "Rowan"},
            ).to_json())

            sync = await _recv_until(ws2, "state_sync")
            rook_view = sync.payload["characters"][player1_id]
            assert rook_view["name"] == "Rook"
            assert rook_view["hp"] == rook_view["max_hp"] == 12  # fighter's d10 hit die max (10) + CON modifier (+2)
            assert "inventory" not in rook_view, "a real second connection must never receive another player's inventory"

            # A live player_joined broadcast for Rowan should also reach
            # Rook's still-open connection.
            joined = await _recv_until(ws1, "player_joined")
            assert joined.payload["player_id"] == player2_id
            assert joined.payload["name"] == "Rowan"

            await ws1.close()  # real socket close - the same event a crashed/quit client produces

            left = await _recv_until(ws2, "player_left")
            assert left.payload == {"player_id": player1_id, "name": "Rook"}
        finally:
            await ws2.close()
            if ws1.close_code is None:
                await ws1.close()
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class NarratesOpeningDM:
    """Stands in for a real LLM backend the same way StubDM does elsewhere
    in this file - this environment has no live Ollama/Anthropic access,
    so this is the strongest verification available: a real client
    (DungeonMasterApp, real ClientTransport, real websocket - no
    ClientTransport mocking, unlike tests/test_client_app.py) driven
    through a real server (real Transport/GameEngine) for the whole
    lobby -> start -> live-streamed-opening-scene flow, with only the
    narration content itself substituted."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "A cold wind sweeps through the village square."


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


async def _wait_until(predicate, timeout: float = 5, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


async def test_real_client_lobby_to_session_flow_over_real_websocket():
    """The one test in this project driving a real client through a real
    server end to end, not a mocked transport on one side or the other -
    catches exactly the class of bug unit-level tests on either side
    can't: a real wire-format mismatch, or the client/engine disagreeing
    about lobby-vs-session sequencing (see GameEngine._on_start_session's
    session_started-before-narration ordering, and the App.query_one()
    default-screen pitfall documented in client/app.py - both found while
    building this feature, neither would show up testing client or server
    in isolation)."""
    session = Session(session_id="e2e-session-3")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, NarratesOpeningDM(), broadcast, send_to, enable_opening_scene=True)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8801))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8801", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            assert isinstance(app.screen, WelcomeScreen)
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")

            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))
            assert isinstance(app.screen, LobbyScreen)

            await pilot.click("#start")

            await _wait_until(lambda: isinstance(app.screen, SessionScreen))
            await _wait_until(lambda: "cold wind" in _log_text(app.screen.query_one("#log")))
            assert "cold wind" in _log_text(app.screen.query_one("#log"))
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class NarratesTurnDM:
    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield f"The DM responds to: {action_text}"


class DefeatsGoblinDM:
    """Simulates a DM turn that introduces and immediately kills a goblin -
    "goblin" matches server/rules/srd.json's own monster entry (CR 1/4),
    so this exercises the real automatic CR-to-XP lookup, not just an
    explicit xp override."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update({"target": "goblin", "max_hp": 5, "hp_delta": -5})
        yield "You strike the goblin down."


async def test_defeating_an_npc_awards_xp_and_updates_the_sheet_panel_over_a_real_session():
    """The XP/leveling system's deterministic award-on-kill logic
    (server/engine.py's apply_update closure) is unit-tested against the
    engine directly in tests/test_engine.py - this is the same "run it
    through a real client and a real server, not mocked on either side"
    pass test_two_real_clients_trade_turns_over_a_real_session already
    established, confirming the real wire format (a system_message
    announcing the XP) and the real CharacterSheetPanel rendering (Lv/XP
    lines added this feature) actually connect end to end."""
    session = Session(session_id="e2e-session-5")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, DefeatsGoblinDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8803))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8803", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I attack the goblin", "enter")
            await _wait_until(lambda: "gains 50 XP" in _log_text(app.screen.query_one("#log")))

            assert "reaches level" not in _log_text(app.screen.query_one("#log")), \
                "50 XP shouldn't cross the real level-2 threshold (300)"

            sheet = app.screen.query_one("#sheet")
            rendered = sheet._Static__content
            assert "XP: 50" in rendered
            assert "Lv 1" in rendered
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class RequestsAbilityRollDM:
    """Simulates a DM turn requesting a DEX check - exercises the real
    ability-modifier plumbing (server/engine.py's request_roll closure,
    server/state.py's stat_modifiers computed field) over an actual
    websocket, not just at the engine-unit level."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        request_roll({"dice": "1d20", "dc": 10, "reason": "tumble past the guard", "ability": "dex"})
        yield "You attempt to slip past the guard."


async def test_ability_score_modifier_applies_to_a_dm_requested_roll_over_a_real_session():
    """Confirms the whole ability-score chain works end to end over a real
    websocket: build_starting_character generates real stats for a fighter,
    the sheet panel renders them, and a DM-requested roll tied to one of
    those abilities gets the real, correctly-computed modifier - not a
    mocked stat_modifiers dict like the client-level test uses."""
    session = Session(session_id="e2e-session-7")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, RequestsAbilityRollDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8805))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8805", player_id=player_id, is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#class-input")
            await pilot.press(*"fighter")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            sheet = app.screen.query_one("#sheet")
            rendered = sheet._Static__content
            assert "Ability Scores" in rendered
            assert "DEX 13 (+1)" in rendered  # fighter's real Standard Array assignment

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I try to slip past the guard", "enter")
            await _wait_until(lambda: "+1 DEX" in _log_text(app.screen.query_one("#log")))

            # A real, direct check on server state - the roll the DM
            # actually requested really did carry a genuine, correct +1.
            assert session.characters[player_id].stat_modifiers["dex"] == 1
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class RequestsWeaponDamageRollDM:
    """Simulates a DM turn narrating a hit and rolling real weapon damage -
    exercises the real equipment-lookup plumbing (server/engine.py's
    request_roll closure resolving a real srd.json damage die) over an
    actual websocket."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        request_roll({"weapon": "longsword", "ability": "str", "reason": "damage roll"})
        yield "Your longsword bites deep."


async def test_structured_equipment_ac_and_weapon_damage_over_a_real_session():
    """Confirms the structured-equipment chain works end to end over a real
    websocket: a fighter's real starting AC (leather armor + DEX) renders
    on the sheet, and a DM-requested weapon damage roll resolves the real
    SRD damage die/type - not mocked payload dicts like the client-level
    tests use."""
    session = Session(session_id="e2e-session-8")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, RequestsWeaponDamageRollDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8806))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8806", player_id=player_id, is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#class-input")
            await pilot.press(*"fighter")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            sheet = app.screen.query_one("#sheet")
            rendered = sheet._Static__content
            assert "AC 12" in rendered  # leather armor's real base (11) + fighter's real DEX mod (+1)

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I strike with my longsword", "enter")
            await _wait_until(lambda: "(slashing)" in _log_text(app.screen.query_one("#log")))

            log_text = _log_text(app.screen.query_one("#log"))
            assert "1d8 (slashing) +2 STR" in log_text  # real longsword die + fighter's real STR mod

            # A real, direct check on server state.
            assert session.characters[player_id].ac == 12
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class RequestsRollWhilePoisonedDM:
    """Simulates a DM turn requesting a roll while the acting character is
    already poisoned - exercises the real automatic-disadvantage plumbing
    (server/engine.py's request_roll closure, _has_disadvantage) over an
    actual websocket."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update({"add_condition": "poisoned"})
        request_roll({"dice": "1d20", "dc": 10, "reason": "stealth check"})
        yield "Sickened by the poison, you try to move quietly anyway."


async def test_mechanical_conditions_disadvantage_applies_over_a_real_session():
    """Confirms the disadvantage chain works end to end over a real
    websocket: a real add_condition call tracks "poisoned" on the real
    server-side sheet, and the very next request_roll in the same turn
    picks it up and rolls with real disadvantage - not a pre-seeded
    condition or a mocked dice.roll() like the unit-level tests use."""
    session = Session(session_id="e2e-session-9")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, RequestsRollWhilePoisonedDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8807))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8807", player_id=player_id, is_new_character=True)
        with patch("server.dice.random.randint", side_effect=[18, 4]):
            async with app.run_test() as pilot:
                await pilot.click("#name-input")
                await pilot.press(*"Thrain")
                await pilot.click("#join")
                await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

                await pilot.click("#start")
                await _wait_until(lambda: isinstance(app.screen, SessionScreen))

                await pilot.click("#input")
                await pilot.press(*"I try to move quietly despite the poison", "enter")
                await _wait_until(lambda: "disadvantage: poisoned" in _log_text(app.screen.query_one("#log")))

                log_text = _log_text(app.screen.query_one("#log"))
                assert ": 4 [18, 4]" in log_text  # kept the lower roll (4), both real rolls shown

                # A real, direct check on server state.
                assert "poisoned" in session.characters[player_id].conditions
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def test_character_edit_notes_and_inventory_over_a_real_session():
    """Confirms /note and /item add/remove genuinely round-trip through a
    real websocket session, out of turn: character_edit is handled the
    instant it arrives (no DM/narrate() call involved at all, unlike
    player_action), and the resulting private character_update re-renders
    the real client's own sheet panel with no narration in the loop."""
    session = Session(session_id="e2e-session-10")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, StubDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8808))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8808", player_id=player_id, is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"/item add a shiny rock", "enter")
            await _wait_until(lambda: "a shiny rock" in app.screen.query_one("#sheet")._Static__content)

            await pilot.click("#input")
            await pilot.press(*"/note the old man owes me a favor", "enter")
            await pilot.pause()

            # A real, direct check on server state - notes never render
            # client-side, so the sheet panel alone can't confirm this half.
            assert session.characters[player_id].notes == "the old man owes me a favor"
            assert "a shiny rock" in session.characters[player_id].inventory
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class DealsLethalSelfDamageDM:
    """Simulates a DM turn that drops the acting character straight to 0
    HP - target omitted defaults to "self" (server/engine.py's apply_update
    closure), the same as any ordinary hp_delta call."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update({"hp_delta": -100})
        yield "The blow lands hard and you crumple to the ground."


async def test_death_saves_over_a_real_session():
    """Confirms the whole death-save chain works end to end over a real
    websocket, not just at the engine-unit level (tests/test_engine.py):
    a real hp_delta drops a real client's character to 0 HP and into
    dying (announced via a real system_message), a normal player_action is
    genuinely rejected while down rather than silently no-oping, and a
    real /deathsave roll (mocked dice for a deterministic natural-20
    outcome) revives the character with 1 HP and clears dying - all
    reflected in the real server-side Session state and the real client's
    own re-rendered sheet panel."""
    session = Session(session_id="e2e-session-11")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, DealsLethalSelfDamageDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8810))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8810", player_id=player_id, is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I charge the ogre head-on", "enter")
            await _wait_until(lambda: "is dying" in _log_text(app.screen.query_one("#log")))
            assert session.characters[player_id].dying is True

            # A normal action while down is genuinely rejected - a real
            # round trip, not just the unit-level rejection test.
            await pilot.click("#input")
            await pilot.press(*"I try to stand up", "enter")
            await _wait_until(lambda: "not a normal action" in _log_text(app.screen.query_one("#log")))
            assert session.characters[player_id].hp == 0  # untouched by the rejected action

            with patch("server.dice.random.randint", return_value=20):
                await pilot.click("#input")
                await pilot.press(*"/deathsave", "enter")
                await _wait_until(lambda: "claws back to consciousness" in _log_text(app.screen.query_one("#log")))

            assert session.characters[player_id].hp == 1
            assert session.characters[player_id].dying is False
            sheet = app.screen.query_one("#sheet")
            assert "HP 1/" in sheet._Static__content
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def test_transcript_command_saves_a_real_session_over_a_real_websocket(tmp_path):
    """/transcript is entirely client-side (no protocol envelope at all -
    see docs/protocol.md), but the log it reads from is only real once a
    real client has actually streamed real narration into it over a real
    connection, not a FakeTransport-driven unit test."""
    session = Session(session_id="e2e-session-11")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, NarratesOpeningDM(), broadcast, send_to, enable_opening_scene=True)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8809))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8809", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))
            await _wait_until(lambda: "cold wind" in _log_text(app.screen.query_one("#log")))

            transcript_path = tmp_path / "e2e-session-11"
            await pilot.click("#input")
            await pilot.press(*f"/transcript {transcript_path}", "enter")
            await _wait_until(lambda: (tmp_path / "e2e-session-11.txt").exists())

            written = (tmp_path / "e2e-session-11.txt").read_text()
            assert "cold wind" in written
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def test_lobby_transcript_command_saves_real_chat_over_a_real_websocket(tmp_path):
    """/transcript in the lobby reads #chat-log specifically - confirms it
    against a real two-player lobby chat exchange over a real websocket,
    not just a single FakeTransport-driven client."""
    session = Session(session_id="e2e-session-14")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, StubDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8812))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app1 = DungeonMasterApp(uri="ws://localhost:8812", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app1.run_test() as pilot1:
            await pilot1.click("#name-input")
            await pilot1.press(*"Thrain")
            await pilot1.click("#join")
            await _wait_until(lambda: isinstance(app1.screen, LobbyScreen))

            ws2 = await connect("ws://localhost:8812")
            try:
                await ws2.send(Envelope(
                    type="join_session", session_id="e2e-session-14", sender_id=str(uuid.uuid4()),
                    payload={"player_name": "Rowan"},
                ).to_json())
                await ws2.send(Envelope(
                    type="chat_message", session_id="e2e-session-14", sender_id=str(uuid.uuid4()),
                    payload={"text": "ready when you are"},
                ).to_json())

                await _wait_until(lambda: "ready when you are" in _log_text(app1.screen.query_one("#chat-log")))

                transcript_path = tmp_path / "e2e-lobby-chat"
                await pilot1.click("#chat-input")
                await pilot1.press(*f"/transcript {transcript_path}", "enter")
                await _wait_until(lambda: (tmp_path / "e2e-lobby-chat.txt").exists())

                written = (tmp_path / "e2e-lobby-chat.txt").read_text()
                assert "ready when you are" in written
            finally:
                await ws2.close()
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class NarratesLethalDamageWithNoToolCallDM:
    """Reconstructs the exact real failure shape ROADMAP.md's tool-call
    reliability investigation documents - narration confirms lethal damage
    with no update_character call at all - to exercise the missed-change
    heuristic's advisory system_message over a real websocket, not just the
    mocked-DM unit tests in test_engine.py."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        yield "Your blade finds its mark - the bandit staggers, bleeding, and falls dead."


async def test_missed_change_advisory_renders_with_distinct_styling_over_a_real_session():
    session = Session(session_id="e2e-session-12")

    def engine_factory(broadcast, send_to):
        return GameEngine(
            session, NarratesLethalDamageWithNoToolCallDM(), broadcast, send_to, enable_opening_scene=False
        )

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8810))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8810", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I strike the bandit", "enter")
            await _wait_until(lambda: "out of sync" in _log_text(app.screen.query_one("#log")))

            log = app.screen.query_one("#log")
            assert any("yellow" in str(seg.style) for strip in log.lines for seg in strip._segments)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class IntroducesHostileGoblinDM:
    """Simulates a DM turn introducing a new NPC with a real disposition -
    exercises the disposition field over an actual websocket session, not
    just the mocked-DM unit test in test_engine.py."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update({"target": "goblin", "max_hp": 7, "disposition": "hostile"})
        yield "A goblin bursts from the underbrush, weapon raised."


async def test_npc_disposition_over_a_real_session():
    session = Session(session_id="e2e-session-13")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, IntroducesHostileGoblinDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8811))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8811", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I peer into the underbrush", "enter")
            await _wait_until(lambda: "hostile" in _log_text(app.screen.query_one("#log")))

            # A real, direct check on server state.
            assert session.npcs["goblin"].disposition == "hostile"
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


class SequentialNPCUpdatesDM:
    """Applies one update_character call per narrate() invocation, in
    order - simulates the same goblin taking damage across two separate
    real turns, to confirm the client's persistent NPCs panel reflects the
    latest state (one entry, current HP) rather than the old dim-log-line
    behavior of just accumulating stale text."""

    def __init__(self, updates: list[dict]):
        self._updates = updates
        self._index = 0

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update(self._updates[self._index])
        self._index += 1
        yield "The goblin reacts."


async def test_npc_status_panel_stays_current_across_real_turns():
    session = Session(session_id="e2e-session-15")
    dm = SequentialNPCUpdatesDM([
        {"target": "goblin", "max_hp": 7, "hp_delta": -4},
        {"target": "goblin", "hp_delta": -3},
    ])

    def engine_factory(broadcast, send_to):
        return GameEngine(session, dm, broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8813))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8813", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I strike the goblin", "enter")
            await _wait_until(lambda: "goblin" in app.screen.query_one("#sheet")._Static__content)

            rendered = app.screen.query_one("#sheet")._Static__content
            assert "HP 3/7" in rendered
            assert "(defeated)" not in rendered

            await pilot.click("#input")
            await pilot.press(*"I strike the goblin again", "enter")
            await _wait_until(lambda: "(defeated)" in app.screen.query_one("#sheet")._Static__content)

            rendered = app.screen.query_one("#sheet")._Static__content
            assert rendered.count("goblin") == 1  # updated in place, not duplicated
            assert "HP 0/7" in rendered
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def test_character_import_over_a_real_session_wins_over_typed_name_and_class(tmp_path):
    """Character export is purely client-side (no server involvement, see
    DungeonMasterApp.export_character) so it doesn't need a websocket to
    verify - but import is a real join_session round trip, so this is the
    one real end-to-end check: a real DungeonMasterApp reads a real local
    export file via WelcomeScreen's #import-input, sends it as a real
    join_session payload, and the real GameEngine on the other end of an
    actual websocket builds the session character from it rather than
    from whatever was typed into #name-input/#class-input."""
    export_path = tmp_path / "torvin.json"
    export_path.write_text(json.dumps({
        "player_id": "stale-id-from-a-previous-session",
        "name": "Torvin Ironheart", "hp": 9, "max_hp": 14, "character_class": "Cleric",
        "inventory": ["Mace", "Holy Symbol"], "xp": 450, "level": 3,
    }))

    session = Session(session_id="e2e-session-6")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, StubDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8804))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player_id = str(uuid.uuid4())
        app = DungeonMasterApp(uri="ws://localhost:8804", player_id=player_id, is_new_character=True)
        async with app.run_test() as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Someone Else")  # should be ignored - the import wins
            await pilot.click("#import-input")
            await pilot.press(*str(export_path))
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            assert session.characters[player_id].player_id == player_id
            assert session.characters[player_id].name == "Torvin Ironheart"
            assert session.characters[player_id].xp == 450
            assert session.characters[player_id].level == 3

            sheet = app.screen.query_one("#sheet")
            rendered = sheet._Static__content
            assert "Torvin Ironheart" in rendered
            assert "Lv 3" in rendered
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


async def test_two_real_clients_trade_turns_over_a_real_session():
    """ROADMAP.md: the architecture has always supported multiple players
    (turn_order is a list, the transport handles multiple connections),
    and a real 2-connection join/presence/disconnect pass exists, but
    nobody had actually taken two real clients through a session trading
    real player_actions yet. This is that pass - two real
    DungeonMasterApps, real ClientTransports, one real server, no
    ClientTransport mocking."""
    session = Session(session_id="e2e-session-4")

    def engine_factory(broadcast, send_to):
        return GameEngine(session, NarratesTurnDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8802))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        player1 = DungeonMasterApp(uri="ws://localhost:8802", player_id=str(uuid.uuid4()), is_new_character=True)
        player2 = DungeonMasterApp(uri="ws://localhost:8802", player_id=str(uuid.uuid4()), is_new_character=True)

        async with player1.run_test() as pilot1, player2.run_test() as pilot2:
            await pilot1.click("#name-input")
            await pilot1.press(*"Thrain")
            await pilot1.click("#join")
            await _wait_until(lambda: isinstance(player1.screen, LobbyScreen))

            await pilot2.click("#name-input")
            await pilot2.press(*"Rowan")
            await pilot2.click("#join")
            await _wait_until(lambda: isinstance(player2.screen, LobbyScreen))

            await pilot1.click("#start")
            await _wait_until(lambda: isinstance(player1.screen, SessionScreen))
            await _wait_until(lambda: isinstance(player2.screen, SessionScreen))

            # Thrain joined first, so turn_order[0] is Thrain. Both clients
            # get the same broadcast turn_prompt - a real, previously-
            # unexercised gap only surfaced by running two real clients:
            # the non-active player used to get no indication at all of
            # whose turn it was.
            await _wait_until(lambda: "Your turn" in _log_text(player1.screen.query_one("#log")))
            await _wait_until(lambda: "Thrain's turn" in _log_text(player2.screen.query_one("#log")))

            # Rowan acting right now is genuinely out of turn.
            await pilot2.click("#input")
            await pilot2.press(*"I peek through the keyhole", "enter")
            await _wait_until(
                lambda: "not your turn" in _log_text(player2.screen.query_one("#log")).lower()
            )
            assert "keyhole" not in _log_text(player1.screen.query_one("#log")), \
                "an out-of-turn action must never reach the other player's log"

            await pilot1.click("#input")
            await pilot1.press(*"I open the door", "enter")
            await _wait_until(lambda: "open the door" in _log_text(player2.screen.query_one("#log")))

            # Both clients should see Thrain's action and the DM's response
            # to it - action/narration broadcast to everyone, not just the
            # acting player.
            for screen_owner in (player1, player2):
                log_text = _log_text(screen_owner.screen.query_one("#log"))
                assert "Thrain: I open the door" in log_text
                assert "The DM responds to: I open the door" in log_text

            await _wait_until(lambda: "Your turn" in _log_text(player2.screen.query_one("#log")))
            assert "Rowan's turn" in _log_text(player1.screen.query_one("#log"))

            # Turn has now passed to Rowan - her real player_action should
            # be accepted and, this time, broadcast to both.
            await pilot2.click("#input")
            await pilot2.press(*"I step through the doorway", "enter")
            await _wait_until(lambda: "step through the doorway" in _log_text(player1.screen.query_one("#log")))

            for screen_owner in (player1, player2):
                log_text = _log_text(screen_owner.screen.query_one("#log"))
                assert "Rowan: I step through the doorway" in log_text
                assert "The DM responds to: I step through the doorway" in log_text

            # /roll is exempt from turn order and structured client-side
            # rendering (dice_result) is new - a real roll over a real
            # socket should reach both clients exactly once each, not as a
            # duplicate of the plain log_entry text.
            await pilot2.click("#input")
            await pilot2.press(*"/roll 1d20 perception check", "enter")
            await _wait_until(lambda: "rolls 1d20" in _log_text(player1.screen.query_one("#log")))

            for screen_owner in (player1, player2):
                log_text = _log_text(screen_owner.screen.query_one("#log"))
                assert "Rowan rolls 1d20 (perception check)" in log_text
                assert log_text.count("rolls 1d20") == 1
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
