# Roadmap

A status snapshot and prioritized next steps — not a promise or a schedule, just a living plan for a solo project. This repo stays private until the owner decides otherwise.

## Current state (done)

- Single-player skeleton: WebSocket server + Textual client, strict turn queue.
- Persistence: session state survives server restarts; the client remembers its own identity across restarts so it reconnects as the same character.
- Chat (`/chat <text>`) and dice rolls (`/roll <NdM[+/-K]> [reason]`) wired end-to-end, both exempt from turn order.
- DM narration behind a swappable `NarratorBackend` (`server/narrator.py`), currently `AnthropicNarrator` using Claude with tool use: a local SRD lookup tool (`server/rules/`, CC-BY-4.0 dataset) for consistent mechanics, and hosted web search for general inspiration only.
- **DM memory**: a rolling window of the last 6 turns (`Session.append_turn`, `MAX_HISTORY_MESSAGES` in `server/state.py`) is threaded through as real conversation history on every `narrate()` call, replacing the old single-last-turn-only context. Chosen over full history (unbounded cost growth) and a summary hybrid (more complexity than needed right now) explicitly because of the no-spend stance — see the decision note below.
- CI, a 31-test suite, MIT license (with the SRD data separately CC-BY-4.0 attributed), issue/PR templates.
- **Not yet verified**: an actual successful narration call against the live Claude API. Everything up to the request is tested and confirmed working; there's no account credit to see a real response yet — that's a deliberate choice, not an oversight.

## Next — highest priority, in rough order

1. **Make narrated outcomes actually change the character sheet.** HP and inventory are set once at character creation and never touched again — the DM can *say* "you take 4 damage," but nothing enforces it. There's no real stakes or persistent consequence yet, just narration. Given the tool-use architecture already in place, the natural fix is an `update_character` tool (HP delta, add/remove inventory, add/remove a condition) the DM calls after narrating an outcome, which the engine applies and then emits as `character_update` to the player plus a `log_entry` to the room — `character_update` is already defined in `docs/protocol.md` and currently unused.

2. **Verify a real narration round-trip.** Either add Anthropic credits, or build a local-model `NarratorBackend` (the swap point already exists — see `create_narrator()` in `server/narrator.py`). Right now the entire pipeline up to the API call is tested and confirmed correct, including the new history wiring; nobody has actually seen the DM speak yet.

3. **Revisit memory if sessions run long.** The rolling window (last 6 turns) fixes the "forgets what just happened" problem completely but still forgets anything older than the window — a very long session could still see the DM drop an early plot thread or NPC. Not worth solving until it's actually observed happening in play; the fix, if needed, is a summary of everything outside the window, kept updated via a DM tool call the same way `update_character` (above) would work.

## Later — planned, lower priority

- **Multiplayer**: the architecture supports multiple players (`turn_order` is already a list, the transport already handles multiple connections), but it's never been exercised with 2+ concurrent connections. Worth a deliberate test pass before calling it actually supported.
- **`character_edit`** (protocol-defined, unimplemented): let a player edit their own notes/inventory directly, without DM adjudication.
- **Expand the SRD dataset** (`server/rules/srd.json`) — more monsters, spells, and full class progressions, staying strictly within CC-BY-4.0-licensed SRD content.
- **Image generation** for scenes/characters, triggered off narration beats.
- **Text-to-speech** for DM narration.

## Explicitly not doing

- Scraping or bundling copyrighted D&D sourcebook content (Monster Manual, Player's Handbook, Dungeon Master's Guide, published adventures) — not legally redistributable, even in a private repo. See `server/rules/ATTRIBUTION.md` for what's actually included and why.
- Making the repo public on any automatic trigger — that's the owner's call, not tied to any milestone here.
