"""One-off recapture of the two genuinely live screenshots (opening-scene.svg,
turn-in-progress.svg) - unlike scripts/generate_screenshots.py's three mocked
captures, these need a real running server with a real Ollama/Anthropic
backend behind it, so this isn't meant to run in CI or by a contributor
without live model access. Kept in version control anyway so the exact
capture steps are reproducible the next time these need a real refresh
(a TUI-visible change, a new model, etc.) rather than living only in
whoever's shell history happened to run them.

Point SERVER_URI at a real running `python -m server.main` (DM_BACKEND
already configured on that server, not here) before running:

    SERVER_URI=ws://<host>:8765 python -m scripts.generate_live_screenshots
"""

from __future__ import annotations

import asyncio
import os
import uuid

from client.app import DungeonMasterApp, LobbyScreen, SessionScreen

SCREENSHOT_DIR = "docs/screenshots"
SIZE = (120, 40)  # matches scripts/generate_screenshots.py's SIZE


async def _wait_until(predicate, timeout: float = 180, interval: float = 0.1) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


def _log_text(rich_log) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


async def main() -> None:
    uri = os.environ.get("SERVER_URI", "ws://localhost:8765")
    # A fresh, never-used session_id each run - a live server's real
    # persisted sessions (e.g. "default") may already have other real
    # characters/history in them, which would make for a confusing,
    # unrepresentative screenshot rather than a clean fresh-game one.
    session_id = f"screenshot-live-{uuid.uuid4().hex[:8]}"

    app = DungeonMasterApp(uri=uri, player_id=str(uuid.uuid4()), is_new_character=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.click("#name-input")
        await pilot.press(*"Thrain")
        await pilot.click("#session-input")
        await pilot.press(*session_id)
        await pilot.click("#class-input")
        await pilot.press(*"fighter")
        await pilot.click("#join")
        await _wait_until(lambda: isinstance(app.screen, LobbyScreen))
        await pilot.pause()  # let the newly-pushed screen finish mounting before clicking into it

        await pilot.click("#start")
        await _wait_until(lambda: isinstance(app.screen, SessionScreen))
        # Real streamed narration from a real model - wait for it to finish,
        # not a fixed sleep, so this doesn't flake against a slower model.
        await _wait_until(lambda: "Your turn." in _log_text(app.screen.query_one("#log")))
        await pilot.pause()
        app.save_screenshot(filename="opening-scene.svg", path=SCREENSHOT_DIR)
        print("Wrote opening-scene.svg")

        await pilot.click("#input")
        await pilot.press(*"I draw my sword and cautiously step deeper into the ruins", "enter")
        await _wait_until(lambda: _log_text(app.screen.query_one("#log")).count("Your turn.") >= 2)
        await pilot.pause()
        app.save_screenshot(filename="turn-in-progress.svg", path=SCREENSHOT_DIR)
        print("Wrote turn-in-progress.svg")


if __name__ == "__main__":
    asyncio.run(main())
