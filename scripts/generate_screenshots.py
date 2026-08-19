"""Regenerates docs/screenshots/*.svg after a client-visible TUI change.

Every screenshot is generated here with a scripted stub DM standing in for
a real model - the same honest distinction this project's own
tests/test_transport_e2e.py already draws between real end-to-end
websocket verification and live-model verification. The two screenshots
that used to be real live-Ollama captures (opening-scene.svg,
turn-in-progress.svg) joined this scripted set when the dark-dungeon theme
recolored the whole client: a live capture can't be regenerated in this
no-live-LLM environment, so leaving them stale with the old palette was
worse than converting them to the same honest "real engine mechanics,
scripted prose" framing the rest already used. The mechanics shown (XP
awarded, a real level-up, HP growth, real spell-slot bookkeeping, a real
update_world setting the Map tab's location, the character sheet panel)
are 100% real engine behavior either way - only the narration *prose*
itself is a stand-in, not the game state changes.

Run from the repo root:

    python -m scripts.generate_screenshots
"""

from __future__ import annotations

import asyncio
import uuid

from client.app import DungeonMasterApp, LobbyScreen, SessionScreen
from server.engine import GameEngine
from server.state import Session
from server.transport import Transport

SCREENSHOT_DIR = "docs/screenshots"
SIZE = (120, 40)  # wider/taller than the 80x24 default - matches this
# project's original two screenshots closely enough for a consistent look,
# and gives the sheet+log side-by-side layout room to breathe.


async def _wait_until(predicate, timeout: float = 5, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


async def generate_welcome_import_screenshot() -> None:
    """WelcomeScreen with the new character-import field visible - pure UI,
    no server connection or narration needed at all, so nothing here is a
    stand-in for anything."""
    app = DungeonMasterApp(uri="ws://unused", player_id=str(uuid.uuid4()), is_new_character=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.save_screenshot(filename="welcome-import.svg", path=SCREENSHOT_DIR)


class CastsSpellDM:
    """A scripted stand-in for a real DM - see the module docstring for why
    this is honestly distinguished from the two live-Ollama screenshots.
    Spends a real 1st-level spell slot via the new cast_spell field
    (server/engine.py's _cast_spell) rather than guessing at slot
    bookkeeping - the same explicit-tool-call path a real DM would use."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        apply_update({"cast_spell": "magic_missile"})
        yield "Arcane light gathers at your fingertips - three glowing darts of force streak out and slam home."


class LevelsUpDM:
    """A scripted stand-in for a real DM - see the module docstring for why
    this is honestly distinguished from the two live-Ollama screenshots.
    Kills a "dire wolf" NPC with an explicit xp override (300, exactly the
    real level-2 threshold) so the level-up fires deterministically,
    regardless of any SRD Challenge-Rating lookup - the same explicit-
    override path a real DM narrating a tougher-than-usual foe would use."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        apply_update({"target": "dire wolf", "max_hp": 20, "hp_delta": -20, "xp": 300})
        yield "Your blade finds its mark, and the dire wolf collapses."


class OpeningSceneDM:
    """A scripted stand-in for a real DM's opening scene - see the module
    docstring for why this is honestly distinguished from a live capture.
    Sets a real location/campaign summary via update_world so the client's
    Map tab has real data to render, then narrates the setting - no
    mechanical change, the same no-call turn the missed-change heuristic
    correctly ignores on turn zero."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        update_world({"location": "The Veil's Threshold", "summary": "Aetherfall, the realm beyond the Veil."})
        yield "You wake on cold stone at the edge of Aetherfall. Ashwren, the Warden of the Veil, watches from the shadows."


class StoryDM:
    """A scripted stand-in for a mid-story turn - see the module docstring
    for the same honesty note. A plain, non-mechanical narration responding
    to the player's action: the "action echoed, DM responds, your turn"
    shape the turn-in-progress screenshot exists to show."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
        yield "The taproom falls quiet as you step inside. A barmaid glances up, and the hooded figure in the corner shifts in their seat."


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


async def generate_opening_scene_screenshot() -> None:
    """SessionScreen right after the engine's opening scene streams in -
    the turn-in-progress screenshot's setup shot. Uses a real
    GameEngine with enable_opening_scene=True so the DM's update_world call
    really sets the Map tab's location, then captures before the first
    player action."""
    session = Session(session_id="screenshot-session-opening")

    def engine_factory(session_id, broadcast, send_to):
        return GameEngine(session, OpeningSceneDM(), broadcast, send_to, enable_opening_scene=True)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8902))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8902", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Kael")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))
            await _wait_until(lambda: "Aetherfall" in _log_text(app.screen.query_one("#log")))
            await pilot.pause()

            app.save_screenshot(filename="opening-scene.svg", path=SCREENSHOT_DIR)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def generate_turn_in_progress_screenshot() -> None:
    """SessionScreen after a player action, mid-story - action echoed into
    the log, the DM's narrated response streamed in, the next turn prompt.
    The same honest "real engine, scripted prose" shape as the other
    scripted captures."""
    session = Session(session_id="screenshot-session-turn")

    def engine_factory(session_id, broadcast, send_to):
        return GameEngine(session, StoryDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8903))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8903", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Kael")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I step into the taproom", "enter")
            await _wait_until(lambda: "taproom" in _log_text(app.screen.query_one("#log")))
            await pilot.pause()

            app.save_screenshot(filename="turn-in-progress.svg", path=SCREENSHOT_DIR)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def generate_xp_level_up_screenshot() -> None:
    """SessionScreen after a real level-up - real engine-driven XP award,
    real HP growth, real character sheet panel rendering (Lv/XP lines),
    only the narration line itself is scripted rather than model-generated."""
    session = Session(session_id="screenshot-session")

    def engine_factory(session_id, broadcast, send_to):
        return GameEngine(session, LevelsUpDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8900))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8900", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Thrain")
            await pilot.click("#class-input")
            await pilot.press(*"fighter")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I strike down the dire wolf", "enter")
            await _wait_until(lambda: "reaches level" in _log_text(app.screen.query_one("#log")))
            await pilot.pause()

            app.save_screenshot(filename="xp-level-up.svg", path=SCREENSHOT_DIR)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def generate_spellcasting_screenshot() -> None:
    """SessionScreen after casting a real leveled spell - real engine-driven
    spell-slot bookkeeping (2/2 -> 1/2 on the character sheet panel), only
    the narration line itself is scripted rather than model-generated."""
    session = Session(session_id="screenshot-session-spellcasting")

    def engine_factory(session_id, broadcast, send_to):
        return GameEngine(session, CastsSpellDM(), broadcast, send_to, enable_opening_scene=False)

    transport = Transport(engine_factory)
    server_task = asyncio.create_task(transport.serve(host="localhost", port=8901))
    await asyncio.sleep(0.3)  # let the server bind

    try:
        app = DungeonMasterApp(uri="ws://localhost:8901", player_id=str(uuid.uuid4()), is_new_character=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.click("#name-input")
            await pilot.press(*"Elowen")
            await pilot.click("#class-input")
            await pilot.press(*"wizard")
            await pilot.click("#join")
            await _wait_until(lambda: isinstance(app.screen, LobbyScreen))

            await pilot.click("#start")
            await _wait_until(lambda: isinstance(app.screen, SessionScreen))

            await pilot.click("#input")
            await pilot.press(*"I cast Magic Missile at the goblin", "enter")
            await _wait_until(lambda: "darts" in _log_text(app.screen.query_one("#log")))
            await pilot.pause()

            app.save_screenshot(filename="spellcasting.svg", path=SCREENSHOT_DIR)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def main() -> None:
    await generate_welcome_import_screenshot()
    await generate_opening_scene_screenshot()
    await generate_turn_in_progress_screenshot()
    await generate_xp_level_up_screenshot()
    await generate_spellcasting_screenshot()
    print(f"Wrote opening-scene.svg, turn-in-progress.svg, welcome-import.svg, xp-level-up.svg, and spellcasting.svg to {SCREENSHOT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
