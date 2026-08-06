# Roadmap

A status snapshot and prioritized next steps — not a promise or a schedule, just a living plan for a solo project. This repo stays private until the owner decides otherwise.

**This file is the durable source of truth for project state and decisions** — kept deliberately thorough so that pulling this repo fresh (a different machine, a new AI coding session with no memory of prior conversations, etc.) is enough to pick up where things left off without re-deriving context. If you're an AI assistant reading this cold: read this whole file plus `docs/protocol.md` before making architectural changes: several of the "obvious" simplifications (e.g. "just always call `update_character`", "just use full history") were deliberately rejected below, with reasons.

## Current state (done)

- Single-player skeleton: WebSocket server + Textual client, strict turn queue.
- Persistence: session state survives server restarts; the client remembers its own identity across restarts so it reconnects as the same character.
- Chat (`/chat <text>`) and dice rolls (`/roll <NdM[+/-K]> [reason]`) wired end-to-end, both exempt from turn order.
- DM narration behind a swappable `NarratorBackend` (`server/narrator.py`): `AnthropicNarrator` (hosted Claude) and `OllamaNarrator` (`server/narrator_ollama.py`, free local model). Both support tool use: a local SRD lookup tool (`server/rules/`, CC-BY-4.0 dataset) for consistent mechanics, and `update_character` for real HP/inventory/condition changes. Only `AnthropicNarrator` also has `web_search` — that's an Anthropic-hosted server tool with no local equivalent.
- **DM memory**: a rolling window of the last 6 turns (`Session.append_turn`, `MAX_HISTORY_MESSAGES` in `server/state.py`) is threaded through as real conversation history on every `narrate()` call, replacing the old single-last-turn-only context. Chosen over full history (unbounded cost growth) and a summary hybrid (more complexity than needed right now) explicitly because of the no-spend stance.
- **Real consequences**: an `update_character` tool (HP delta, add/remove inventory, add/remove a condition) the DM calls whenever narration should mechanically change the acting character. The engine applies it via `CharacterSheet.apply_update()` and pushes `character_update` to that player only when something actually changed (a no-op call, e.g. `hp_delta: 0`, doesn't spam an update). `CharacterSheet` gained a `conditions` field for this; the client sheet panel now renders it.
- CI, a 44-test suite, MIT license (with the SRD data separately CC-BY-4.0 attributed), issue/PR templates.
- **A real narration round-trip has been verified**, live, via Ollama — not just mocked, across five real turns total, two models compared head to head on the *same session* (same character "Torvin Ironheart", same rolling history):
  - **`llama3.1:8b`** — turn 1: correct, real `update_character` call (`hp_delta: -1`, the model's own judgment, no injury explicitly prompted). Turn 2, given "I approach the hooded figure and ask what they are drinking": did **not** call the tool at all — instead wrote its reasoning about calling the tool as plain narration text, including a literal fake `{"name": "update_character", "parameters": {...}}` JSON blob that leaked straight into the shared log, and broke character ("To answer this question, we need to update the character's sheet...") despite the system prompt explicitly saying never to. Confirmed via the server log that only one API request was made that turn (a real tool round-trip needs two) — Ollama's actual function-calling mechanism was never invoked; the model just role-played having a tool.
  - **`qwen2.5:7b`** — tried next, on the same session/character (now at HP 9/10 after the llama3.1 turns), specifically to see if a different model was more reliable at this. Turn 3, "I draw my sword and attack the hooded figure": correct, real tool call, `hp_delta: -4`, narration coherent (a magical flash, a shallow wound, the figure escaping), no leaked JSON, stayed in character. Turn 4, "I sheath my sword and look around" (deliberately non-mechanical, to test *precision* not just recall): correctly made **no** tool call — single API request, HP unchanged, clean narration. Turn 5, "I press a cloth against my wound to try to stop the bleeding" (a genuinely ambiguous case — first aid stops bleeding but doesn't typically restore HP without a proper check/spell): again correctly made **no** tool call. Three out of three clean, including one non-trivial judgment call, not just easy no-ops.
  - Minor style quirk noted on turn 5, not a correctness bug: narration ended with a numbered multiple-choice menu ("1. Ask the bartender... 2. Look around...") rather than open-ended prose. The system prompt asks for natural narration inviting the next action, not a menu — worth a prompt tweak if this recurs (e.g. explicitly say "no numbered option lists").
  - Sample size is still small (3 turns for qwen2.5:7b, 2 for llama3.1:8b) — not a rigorous benchmark, just enough signal to change the default. `qwen2.5:7b` is now `OllamaNarrator`'s default model (`server/narrator_ollama.py`, and `.env.example`'s `OLLAMA_MODEL`).
  - CPU-only inference (no GPU on the dev machine — 12 cores, 125GB RAM, integrated Intel graphics only) is slow either way: roughly 30-90s per turn.
  - The hosted Claude backend (`AnthropicNarrator`) shares the identical engine/tool-loop code path but hasn't been run live yet — still pending account credits.

## Next — highest priority, in rough order

1. **Revisit memory if sessions run long.** The rolling window (last 6 turns) fixes the "forgets what just happened" problem completely but still forgets anything older than the window — a very long session could still see the DM drop an early plot thread or NPC. Not worth solving until it's actually observed happening in play; the fix, if needed, is a summary of everything outside the window, kept updated via a DM tool call the same way `update_character` works.

2. **Verify the hosted Claude path live too**, once there's a way to (credits, or just to compare narration quality against the local models side by side) — the code path is identical to Ollama's. Would also be useful as a reliability baseline: Claude is specifically trained for reliable tool use in a way small local models aren't always, so this comparison would show whether `llama3.1:8b`'s turn-2 flakiness (see above) was purely a small-local-model thing or something more general.

3. **`qwen2.5:7b` is now 3-for-3 but still needs a bigger sample before it's fully trusted.** If a future session sees it fail to call `update_character` when it should (or call it when it shouldn't), that's worth logging here the same way the `llama3.1:8b` failure was — don't just quietly patch it. If it keeps checking out over more turns, this item can be closed.

4. **Optional prompt tweak**: discourage numbered multiple-choice menus in narration (see turn 5 above) if it keeps happening — not urgent, just noted while it's fresh.

## Later — planned, lower priority

- **Multiplayer**: the architecture supports multiple players (`turn_order` is already a list, the transport already handles multiple connections), but it's never been exercised with 2+ concurrent connections. Worth a deliberate test pass before calling it actually supported.
- **`character_edit`** (protocol-defined, unimplemented): let a player edit their own notes/inventory directly, without DM adjudication.
- **Expand the SRD dataset** (`server/rules/srd.json`) — more monsters, spells, and full class progressions, staying strictly within CC-BY-4.0-licensed SRD content.
- **Image generation** for scenes/characters, triggered off narration beats.
- **Text-to-speech** for DM narration.

## Explicitly not doing

- Scraping or bundling copyrighted D&D sourcebook content (Monster Manual, Player's Handbook, Dungeon Master's Guide, published adventures) — not legally redistributable, even in a private repo. See `server/rules/ATTRIBUTION.md` for what's actually included and why.
- Making the repo public on any automatic trigger — that's the owner's call, not tied to any milestone here.
