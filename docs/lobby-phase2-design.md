# Tavern lobby — Phase 2 design

Phase 1 (shipped, PR #81): `LobbyScreen`, party roster + presence, tavern chat
(`/me`), ready-check with host-less auto-start, "When last we met…" recap card,
"Review my character", in-tavern long rest (`tavern_rest`).

Phase 2 is two features. Both are lobby-only and never touch the turn queue.

---

## 1. Tavern-keeper NPC — SHIPPED (2026-09-06)

Built as designed. `NarratorBackend.tavern_line(context)` (Anthropic + Ollama),
`GameEngine._maybe_keeper_line` with a 20s `KEEPER_MIN_INTERVAL` rate limit
(one rule covers both greeting and chat), `log_entry` kind `"keeper"`,
`WorldBible.tavern_keeper` (Bruile, in isekai.json). Deferred items below still
deferred.


An AI-voiced greeter that makes the empty text buffer feel like a place:
greets players by name, reacts to tavern chat, hands out the quest board (see
#2). Mostly prompt work.

### Design

- **New optional `NarratorBackend` method: `tavern_line(context) -> str`.**
  Same "optional capability, `getattr` at the call site" convention as
  `summarize` / `check_missed_change`. `AnthropicNarrator` + `OllamaNarrator`
  implement it; a backend without it means no keeper (graceful).
  - `context` is a small dict: `party` (names + race/class), `recent_chat`
    (last ~6 tavern lines), `campaign_summary`, `trigger` (`"greeting"` on the
    first join into a session, or `"chat"` when a player posts).
  - One short paragraph, in-character as the keeper (name from the
    `WorldBible` — Aetherfall's guardian persona, or a dedicated
    `tavern_keeper` field on `WorldBible`, default "the keeper").
  - No tools, no JSON — plain prose, ~40 tokens. Best-effort; failure is
    silent.

- **Server:** `GameEngine` gains a debounced keeper trigger.
  - On a join into a not-started session, after the `player_joined` broadcast:
    if this is the first player OR it's been > N seconds since the last keeper
    line, call `tavern_line({trigger: "greeting", ...})` and broadcast the
    result as `log_entry {kind: "keeper", text}`.
  - On `chat_message` in the lobby: a **rate-limited** keeper reply — at most
    one keeper line per ~20 s of tavern chat, and only when structured_output
    isn't mid-turn (it never is in the lobby). Skip if the message is itself
    addressed to nobody / very short. Keep this conservative: a keeper that
    replies to every line is annoying (research anti-pattern).
  - Never fires once `_has_started()`.

- **Protocol:** new `log_entry` kind `"keeper"` (client → gold/italic, a
  different colour from `chat`). No new envelope type.

- **Client:** `LobbyScreen`'s `TavernChat` renders `keeper` lines with a
  `🍺 {keeperName}:` prefix and distinct styling. That's it.

- **`WorldBible`:** optional `tavern_keeper: {name, persona}` field
  (default → a generic "The keeper, a weathered publican"). Add to
  `aetherfall.json`.

### Cost / risk
Medium. New backend method (2 impls + the Protocol stub), one rate-limiter on
the engine, one `log_entry` kind, one small client render branch. The rate
limit is the part to get right — err toward too quiet. No turn-loop changes.

### Deferred
- Keeper *remembering* players across sessions (needs cross-session store).
- Keeper reacting to `player_ready` / rest ("Off already? Safe travels.") —
  easy add later, not v1.

---

## 2. Quest board

**Prerequisite SHIPPED (2026-09-06):** `end_adventure` / `session_ended` +
`Session.adventures_completed` + `WorldBible.next_adventure_prompt`. The quest
board only makes sense between adventures, and that lobby state didn't exist —
`_has_started()` was a one-way door. It now returns the party to `LobbyScreen`
after `end_adventure`. The board's hook threads into `_on_start_session`'s
`next_adventure_prompt` call (the `hook` param, already wired to accept it).

The DM (AI) posts 2-3 available next missions with a one-line hook; players
signal interest; a mission launches when enough are ready. This replaces the
bare "Start now" with "start *into this hook*".

### Design

- **New `NarratorBackend` method: `quest_hooks(context) -> list[str]`.**
  Optional-capability again. `context`: `party`, `campaign_summary`,
  `world.location`, `completed_objectives` (so hooks build on the story).
  Returns 2-4 one-sentence hooks. Constrained JSON on the Ollama path
  (`{"hooks": [str]}`), plain-ish on Anthropic. Best-effort; on failure the
  board just shows "The road is open — start when ready" (i.e. today's
  behaviour).

- **`Session` gains:** `quest_hooks: list[str]` (generated once per lobby
  visit, persisted), `selected_hook: str | None`, `hook_votes: dict[str, list[str]]`
  (hook text → player_ids interested). Cleared on start.

- **Server events:**
  - `request_quests {}` — any player; generates `quest_hooks` if empty,
    broadcasts `quest_board {hooks, votes}`. Auto-called on first not-started
    `state_sync` if the session has campaign history (a fresh session with no
    story just starts cold, no board).
  - `vote_quest {hook}` — toggles the sender's interest in `hook_votes[hook]`;
    broadcasts the updated `quest_board`. A player votes for at most one hook
    (voting a second clears the first).
  - **Start flow:** when `player_ready` auto-start fires (or `start_session`),
    if a hook has a plurality of votes it becomes `session.selected_hook` and
    is prepended to the opening-scene `action_text` ("The party has decided
    to: {hook}. …"). Otherwise the opening scene is generic as today.

- **Protocol:** `request_quests`, `vote_quest` (client→server);
  `quest_board {hooks: [str], votes: {hook: [player_id]}}` (server→client).

- **Client:** `LobbyScreen` gains a `QuestBoard` panel (below the recap card):
  the hooks as selectable rows, each showing which party members voted (initials),
  a "🔄 New hooks" button (re-`request_quests`). Wires into the existing
  ready bar — "Ready" copy becomes "Ready for: {top hook}" when one leads.

### Cost / risk
Larger. New backend method (2 impls, constrained schema on Ollama), 3 Session
fields, 3 events + 1 broadcast, a `QuestBoard` component, and the opening-scene
`action_text` threading. The AI hook generation is the quality risk — a bad
hook is worse than no board, so the "start generic" fallback must be solid and
the board must be dismissible/ignorable.

### Deferred
- Multiple *concurrent* parties in one tavern picking *different* missions
  (needs the session model to split — big, and contradicts session-id = one
  game). Not doing.
- Hook difficulty / suggested party size tags.
- The DM *reacting* in the opening scene to *who voted for what*.

---

## Build order (post-compact)

1. **Tavern-keeper first** — smaller, and the keeper is the natural thing to
   "hand you the board" in #2's copy. Ship as its own PR stacked on #81.
2. **Quest board** — its own PR stacked on the keeper. Land the `quest_hooks`
   backend method + fallback before any UI.

Both keep the `test_protocol_sync.py` guard honest (add the JS events) and each
new `NarratorBackend` method gets a stub test on `StubDM`.
