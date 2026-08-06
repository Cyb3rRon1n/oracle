# Roadmap

A status snapshot and prioritized next steps — not a promise or a schedule, just a living plan for a solo project. This repo stays private until the owner decides otherwise.

## Current state (done)

- Single-player skeleton: WebSocket server + Textual client, strict turn queue.
- Persistence: session state survives server restarts; the client remembers its own identity across restarts so it reconnects as the same character.
- Chat (`/chat <text>`) and dice rolls (`/roll <NdM[+/-K]> [reason]`) wired end-to-end, both exempt from turn order.
- DM narration behind a swappable `NarratorBackend` (`server/narrator.py`): `AnthropicNarrator` (hosted Claude) and `OllamaNarrator` (`server/narrator_ollama.py`, free local model). Both support tool use: a local SRD lookup tool (`server/rules/`, CC-BY-4.0 dataset) for consistent mechanics, and `update_character` for real HP/inventory/condition changes. Only `AnthropicNarrator` also has `web_search` — that's an Anthropic-hosted server tool with no local equivalent.
- **DM memory**: a rolling window of the last 6 turns (`Session.append_turn`, `MAX_HISTORY_MESSAGES` in `server/state.py`) is threaded through as real conversation history on every `narrate()` call, replacing the old single-last-turn-only context. Chosen over full history (unbounded cost growth) and a summary hybrid (more complexity than needed right now) explicitly because of the no-spend stance.
- **Real consequences**: an `update_character` tool (HP delta, add/remove inventory, add/remove a condition) the DM calls whenever narration should mechanically change the acting character. The engine applies it via `CharacterSheet.apply_update()` and pushes `character_update` to that player only when something actually changed (a no-op call, e.g. `hp_delta: 0`, doesn't spam an update). `CharacterSheet` gained a `conditions` field for this; the client sheet panel now renders it.
- CI, a 44-test suite, MIT license (with the SRD data separately CC-BY-4.0 attributed), issue/PR templates.
- **A real narration round-trip has been verified**, live, against `llama3.1:8b` running locally via Ollama — not just mocked, across two real turns. Turn 1: streamed narration text, a real `update_character` tool call (the model applied `hp_delta: -1` on its own judgment mid-narration), the character sheet updating to match, and correct persistence across the turn. CPU-only inference (no GPU on the dev machine) is slow — roughly 30-90s per turn. The hosted Claude backend (`AnthropicNarrator`) shares the identical engine/tool-loop code path but hasn't been run live yet — still pending account credits.

## Next — highest priority, in rough order

1. **`llama3.1:8b`'s tool-calling reliability is inconsistent — needs attention before this backend is trustworthy for real play.** Turn 1 made a real, correct `update_character` tool call. Turn 2, given "I approach the hooded figure and ask what they are drinking," the model did **not** call the tool at all — instead it wrote its reasoning about calling the tool as plain narration text, including a literal fake `{"name": "update_character", "parameters": {...}}` JSON blob that leaked straight into the shared log, and broke character ("To answer this question, we need to update the character's sheet...") despite the system prompt explicitly saying never to. Confirmed via the server log that only one API request was made that turn — Ollama's own tool-calling mechanism was never actually invoked; the model just role-played having a tool. This is a model-quality problem, not a harness bug — the same request shape worked correctly turn 1. Options to try: a stronger/more explicit system-prompt instruction against narrating meta-commentary, a model better-suited to consistent tool use (`qwen2.5:7b` is often cited as more reliable here), or lowering `temperature` for tool-calling turns. Worth fixing before recommending this backend for actual play rather than just architecture demonstration.

2. **Revisit memory if sessions run long.** The rolling window (last 6 turns) fixes the "forgets what just happened" problem completely but still forgets anything older than the window — a very long session could still see the DM drop an early plot thread or NPC. Not worth solving until it's actually observed happening in play; the fix, if needed, is a summary of everything outside the window, kept updated via a DM tool call the same way `update_character` works.

3. **Verify the hosted Claude path live too**, once there's a way to (credits, or just to compare narration quality against the local model side by side) — the code path is identical to Ollama's. More relevant now than before, given turn 2 above: Claude is specifically trained for reliable tool use in a way an 8B local model isn't, so this comparison would show whether the tool-calling flakiness is a local-model-only problem or something in the shared harness.

## Later — planned, lower priority

- **Multiplayer**: the architecture supports multiple players (`turn_order` is already a list, the transport already handles multiple connections), but it's never been exercised with 2+ concurrent connections. Worth a deliberate test pass before calling it actually supported.
- **`character_edit`** (protocol-defined, unimplemented): let a player edit their own notes/inventory directly, without DM adjudication.
- **Expand the SRD dataset** (`server/rules/srd.json`) — more monsters, spells, and full class progressions, staying strictly within CC-BY-4.0-licensed SRD content.
- **Image generation** for scenes/characters, triggered off narration beats.
- **Text-to-speech** for DM narration.

## Explicitly not doing

- Scraping or bundling copyrighted D&D sourcebook content (Monster Manual, Player's Handbook, Dungeon Master's Guide, published adventures) — not legally redistributable, even in a private repo. See `server/rules/ATTRIBUTION.md` for what's actually included and why.
- Making the repo public on any automatic trigger — that's the owner's call, not tied to any milestone here.
