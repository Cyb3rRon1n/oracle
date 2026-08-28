# Setup walkthrough

Oracle is two halves: a Python **server** (the game engine and the LLM narrator
— owns all game state, computes every mechanical number) and a **web client**
(React, the browser app you actually play in). The server runs on **one**
machine; players connect to it with a browser, from that machine or any other
on the network. Work top to bottom — the client is useless until the server
answers, so get the server up first. The two halves are otherwise independent:
once the server is running, the client section works as written on any machine.

## 1. Prerequisites

- **Server machine**: Python 3.11+, and one LLM backend — either a local
  [Ollama](https://ollama.com) install with a model pulled (`ollama pull
  qwen2.5:7b`) or an API key (Anthropic, or any OpenAI-compatible provider).
  Only the server talks to the LLM; players never need one.
- **Any machine that builds/serves the client**: Node 20+ (build only).
- **Player machines**: just a browser.

## 2. The server (one machine)

```bash
git clone https://github.com/Cyb3rRon1n/oracle.git
cd oracle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[ollama]"        # or ".[dev]" for API-only backends
cp .env.example .env
```

Edit `.env`: set `DM_BACKEND` (`ollama` / `anthropic` / `openai`) and the
matching key or model line. To accept players from *other* machines, also set
`SERVER_HOST=0.0.0.0` — the default `localhost` only answers connections on
this machine.

```bash
python -m server.main
```

**What working looks like**: a log line `Server listening on ws://0.0.0.0:8765`
and the process sitting there quietly. It idles safely with zero clients, and
each session's state saves to `sessions/<session id>.json` as it's played — a
restart resumes where you left off.

## 3. The web client

Build and serve it on any machine — the developer's laptop or the server
machine itself:

```bash
# development, with hot reload
cd web && npm ci
VITE_SERVER_URI=ws://<server-ip-or-hostname>:8765 npm run dev -- --host

# or a production build served by vite's preview server
VITE_SERVER_URI=ws://<server-ip-or-hostname>:8765 npm run build
npm run preview -- --host
```

Three things to know:

- **`VITE_SERVER_URI` is baked in at dev/build time** (`web/src/config.js`) —
  it's how the client finds the server. If the page and the server share a
  machine you can omit it (the default is `ws://localhost:8765`).
- **`--host`** makes vite's dev/preview server answer browsers on other
  machines, not just this one. Find this machine's LAN IP with `ip -4 addr`
  (Linux) or `ipconfig` (Windows).
- If the server machine runs a firewall, open port **8765** (the game
  websocket) and **5173** (the vite page).

**What working looks like**: visiting `http://<that-ip>:5173` from any browser
shows the join screen — "Oracle — An AI Dungeon Master awaits…".

## 4. Your first session

1. Load the client in a browser — one window per player machine, or a couple
   of tabs on one machine for hotseat.
2. Each player picks a name, race, and class. The session id field is
   pre-filled with a fresh random id — leave it, or type the **same id** into
   every window to play together. One tab can also import a character `.json`
   exported from an earlier session.
3. **Join**. Anyone can then hit **Begin the adventure** once the party is
   in, and the DM narrates the opening scene live.
4. Play in plain English. The engine computes HP, AC, XP, spell slots,
   initiative, and disadvantage from tracked conditions — the model narrates,
   it never gets trusted with arithmetic. The scene panel's suggested-action
   chips, the dice tray, and the export buttons are the in-client tools; the
   FR/EN flag switches the interface live.

**What working looks like**: narration streams in after your action, the sheet
tabs reflect real state changes (a hit actually lowers HP), and every window
sees the same story. Between turns the input invites out-of-character chat
when it's not your turn — the engine enforces whose turn it actually is.

Reconnect later: the client remembers your identity and session in
`localStorage` (`oracle_identity`), so a refresh or return drops you straight
back into your character.

## 5. Multiplayer notes

- **Same session id = same game**. The server hosts any number of independent
  sessions side by side, and never leaks one game's traffic into another.
- **Turn order**: the engine enforces it (real DEX-modified initiative); the
  lobby's out-of-character chat works outside turn order.
- **Persistence and reset**: delete `sessions/<session id>.json` to reset a
  world; clear the browser's `oracle_identity` entry to start a fresh
  character.

## 6. Troubleshooting

- **The client can't reach the server** — the three usual causes, in order:
  1. `SERVER_HOST` is still `localhost`, so the server only answers itself.
  2. `VITE_SERVER_URI` was baked pointing at the wrong host (rebuild / re-dev
     with the right one — it doesn't change at runtime).
  3. A firewall on the server machine blocks `8765` / `5173`.
- **Narration is slow** — Ollama on CPU is genuinely slow (30–90 s per
  response; the README calls this normal, and responses stream in as they
  arrive — it's not a hang). A hosted backend is faster.
- **The DM misses warranted state changes sometimes** — small local models do;
  ROADMAP.md documents the whole tool-call reliability investigation and the
  two-phase structured-output mitigation (`OLLAMA_TWO_PHASE`, on by default).
  Hosted backends have been more reliable at this.

See [README.md](../README.md) for the config table, [docs/protocol.md](protocol.md)
for the wire contract, and [docs/REBUILD_PLAN.md](REBUILD_PLAN.md) for what the
v2 rebuild changed and why.