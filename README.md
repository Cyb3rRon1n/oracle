# Oracle

[![CI](https://github.com/Cyb3rRon1n/oracle/actions/workflows/ci.yml/badge.svg)](https://github.com/Cyb3rRon1n/oracle/actions/workflows/ci.yml)

An AI Dungeon Master that runs a real-time tabletop RPG session over a terminal UI — an LLM sitting in the GM seat, adjudicating rules, narrating the world, and managing campaign state.

## Concept

Instead of a single-player chatbot, Oracle is a game engine with an LLM in the GM seat:

- **Multiplayer, two ways** (architecture in place, not yet exercised with 2+ players): players share one terminal and take turns (hotseat), or connect from their own terminals over the network — same engine underneath, different transport.
- **Persistent character view**: each player's client always shows two regions — their character sheet, and a scrolling log of turn actions, DM narration, and prompts.
- **LLM as adjudicator/narrator**: the server owns the source of truth (character sheets, world state, turn order, session log) and calls the LLM to resolve actions and narrate outcomes.
- **Grounded in real rules, not just vibes**: the DM can look up official D&D 5e SRD data (monster stats, spells, conditions) before improvising mechanics, and can reach for general web search when it needs outside inspiration.

## Planned (later)

- Image generation for scene/character art, triggered off narration beats.
- Text-to-speech for DM narration, for a more immersive session.

The narrator sits behind a swappable interface from the start (see Architecture below) specifically so these can slot in without restructuring the engine.

## Architecture

- **Engine/server** (`server/`): owns game state (character sheets, world/campaign state, turn order, session log), enforces a strict turn queue, and calls the LLM for rule adjudication and narration.
- **Narrator** (`server/narrator.py`): the LLM call sits behind a `NarratorBackend` interface, selected via `DM_BACKEND`. Two implementations exist:
  - `AnthropicNarrator` (`DM_BACKEND=anthropic`) — hosted Claude, streams narration and can call three tools mid-turn: a local `lookup_rule` tool backed by a small SRD dataset (`server/rules/`, CC-BY-4.0-licensed — see `server/rules/ATTRIBUTION.md`), `update_character` to apply real HP/inventory/condition changes to the acting character's sheet *or a named NPC/monster's own tracked sheet*, and Anthropic's hosted `web_search` for general inspiration only.
  - `OllamaNarrator` (`server/narrator_ollama.py`, `DM_BACKEND=ollama`) — a free, local backend against a running [Ollama](https://ollama.com) server. Same `lookup_rule`/`update_character` tools; no `web_search` (that's an Anthropic-hosted tool with no local equivalent).
- **Persistence** (`server/persistence.py`): session state is saved to disk via a swappable `SessionStore`, same pattern as the narrator.
- **Clients** (`client/`, Textual TUI): thin — render the character sheet pane + narrative/input log, send player actions to the engine. Hotseat and networked play are the same client/engine pair with different transports (local I/O vs. sockets).
- **Protocol** (`docs/protocol.md`): the client/server event contract — this and the networked-from-day-one split exist so multiplayer, image generation, and TTS can be added later without a rewrite.

## Tech stack

- **Python** + **[Textual](https://textual.textualize.io/)** for the terminal UI.
- **[websockets](https://websockets.readthedocs.io/)** for the client/server transport.
- **[Anthropic Claude](https://www.anthropic.com/claude)** (`claude-sonnet-5`) or a local **[Ollama](https://ollama.com)** model as the narrator, with tool use for grounded rules lookups and real character-state changes.

## Running

With Claude (needs API credits):

```bash
pip install -e .
cp .env.example .env   # fill in ANTHROPIC_API_KEY

# terminal 1
python -m server.main

# terminal 2
python -m client.main
```

With a local model instead (free, no API key — default/tested model is `qwen2.5:7b`; CPU-only inference is slow, expect 30-90s per turn):

```bash
pip install -e ".[ollama]"
# install Ollama (https://ollama.com), then:
ollama pull qwen2.5:7b

# in .env: DM_BACKEND=ollama (OLLAMA_MODEL defaults to qwen2.5:7b)
# same two-terminal run as above
```

Game state (characters, world, turn order, log) is saved to `sessions/<SESSION_ID>.json` after every join and every resolved action, so stopping and restarting the server resumes where you left off. The client remembers its own player ID in a local `.player_id` file, so restarting the client reconnects you to the same character rather than creating a new one — delete that file to start as a fresh character. Delete a session's JSON file under `sessions/` to reset the world itself.

## Testing

```bash
pip install -e ".[dev]"
pytest -v
```

CI (`.github/workflows/ci.yml`) runs the same suite on every push/PR.

## Status

Working single-player game, verified live end-to-end across multiple real turns. Join, character sheet rendering, turn prompts, action submission, chat (`/chat`) and dice rolls (`/roll`), graceful error handling on API failures, full-restart persistence, and streamed narration with real `update_character` tool calls have all been observed working against live local models via Ollama. Two models were compared head to head on the same session: `llama3.1:8b` made a real tool call on turn 1 but lapsed into narrating a fake tool call as plain text on turn 2 (breaking character in the process); `qwen2.5:7b`, tried afterward, correctly called the tool when damage occurred and correctly *didn't* call it when nothing mechanical happened — including a genuinely ambiguous case (first aid vs. real healing) — across three clean turns — see [ROADMAP.md](ROADMAP.md) for the specifics. `qwen2.5:7b` is now the default local model for that reason, though this is still a small sample, not a rigorous benchmark. The hosted Claude backend shares the exact same engine/tool-loop code path and is structurally verified against mocked responses, but hasn't been run live yet — pending Anthropic account credits. `update_character` can now also target a named NPC/monster instead of only the acting character, so a wounded goblin's HP persists turn to turn instead of only ever existing in that turn's prose — built and covered by real engine-level tests, but not yet exercised against a live model. Multiplayer, image generation, and TTS are deliberately not built yet.

See [ROADMAP.md](ROADMAP.md) for what's next and why, in priority order.

## Contributing

Solo portfolio project, not actively seeking contributions, but issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE), except the SRD game data under `server/rules/`, which is CC-BY-4.0 — see [server/rules/ATTRIBUTION.md](server/rules/ATTRIBUTION.md).
