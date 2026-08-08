"""Regenerates docs/screenshots/*.svg after a client-visible TUI change.

The two original screenshots (opening-scene.svg, turn-in-progress.svg)
were captured from a real live Ollama session, no mocked output - this
environment has no live LLM access (see ROADMAP.md throughout), so those
two are NOT regenerated here and must stay as they are until someone with
real Ollama/Anthropic access recaptures them.

The screenshots this script *does* generate are honestly narrower:
welcome-import.svg needs no DM at all (WelcomeScreen renders before any
narration happens), and xp-level-up.svg uses a small scripted stub DM
standing in for a real model - the same honest distinction this project's
own tests/test_transport_e2e.py already draws between real end-to-end
websocket verification and live-model verification. The mechanics shown
(XP awarded, a real level-up, HP growth, the character sheet panel) are
100% real engine behavior either way - only the narration *prose* itself
is a stand-in, not the game state changes.

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


class LevelsUpDM:
    """A scripted stand-in for a real DM - see the module docstring for why
    this is honestly distinguished from the two live-Ollama screenshots.
    Kills a "dire wolf" NPC with an explicit xp override (300, exactly the
    real level-2 threshold) so the level-up fires deterministically,
    regardless of any SRD Challenge-Rating lookup - the same explicit-
    override path a real DM narrating a tougher-than-usual foe would use."""

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        apply_update({"target": "dire wolf", "max_hp": 20, "hp_delta": -20, "xp": 300})
        yield "Your blade finds its mark, and the dire wolf collapses."


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


async def generate_xp_level_up_screenshot() -> None:
    """SessionScreen after a real level-up - real engine-driven XP award,
    real HP growth, real character sheet panel rendering (Lv/XP lines),
    only the narration line itself is scripted rather than model-generated."""
    session = Session(session_id="screenshot-session")

    def engine_factory(broadcast, send_to):
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


async def main() -> None:
    await generate_welcome_import_screenshot()
    await generate_xp_level_up_screenshot()
    print(f"Wrote welcome-import.svg and xp-level-up.svg to {SCREENSHOT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
