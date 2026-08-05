# AI Dungeon Master

A real-time, multiplayer tabletop RPG engine where an LLM acts as the Dungeon Master — adjudicating rules, narrating the world, and managing campaign state for multiple concurrent players.

## Concept

Instead of a single-player chatbot, this is a game engine with an LLM sitting in the GM seat:

- **Multiplayer, two ways**: players can share one terminal and take turns (hotseat), or connect from their own terminals over the network — same engine underneath, different transport.
- **Persistent character view**: each player's client always shows two regions — their character sheet, and a scrolling log of turn actions, DM narration, and prompts.
- **LLM as adjudicator/narrator**: the server owns the source of truth (character sheets, world state, turn order, session log) and calls the LLM to resolve actions and narrate outcomes.

## Planned (later, not v1)

- Image generation for scene/character art, triggered off narration beats.
- Text-to-speech for DM narration, for a more immersive session.

The narrator output path is designed as a pluggable hook from the start so these can slot in later without restructuring the engine.

## Architecture

- **Engine/server**: owns game state (character sheets, world/campaign state, turn order, session log), talks to the LLM for rule adjudication and narration, broadcasts updates to connected clients.
- **Clients (Textual TUI)**: thin — render the character sheet pane + narrative/input log, send player actions to the engine. Hotseat and networked play are the same client/engine pair with different transports (local I/O vs. sockets).

## Tech stack

- **Python** + **[Textual](https://textual.textualize.io/)** for the terminal UI.
- Networked client/server architecture from day one (not hotseat-first, retrofitted later).

## Running (single-player v1)

```bash
pip install -e .
cp .env.example .env   # fill in ANTHROPIC_API_KEY

# terminal 1
python -m server.main

# terminal 2
python -m client.main
```

## Status

Single-player skeleton in place: WebSocket server (`server/`) running the game engine and strict turn queue, calling Claude for narration; Textual client (`client/`) with a character sheet pane and narrative log pane. The narration call sits behind a `NarratorBackend` interface (`server/narrator.py`, selected via `DM_BACKEND`) so a local-model backend can be added later without touching the engine — using the hosted Claude API for now since dev-scale usage is cheap. Multiplayer, persistence, image generation, and TTS are deliberately not built yet — the client/server split and event protocol (`docs/protocol.md`) exist specifically so those can be added without a rewrite.

## License

TBD.
