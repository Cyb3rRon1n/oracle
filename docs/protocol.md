# Client/Server Event Protocol (draft)

Server is the source of truth for game state and turn order. Clients (Textual TUI) are thin: they render two persistent panes — the **narrative log** and the **character sheet** — and send player actions to the server.

## Envelope

Every message, both directions, uses the same wrapper:

```json
{
  "type": "player_action",
  "session_id": "uuid",
  "sender_id": "uuid",
  "ts": "2026-08-05T18:40:00Z",
  "payload": { }
}
```

- `sender_id` is a player id, or `"server"` for system/DM-originated messages.

## Client → Server

| type | payload | purpose |
|---|---|---|
| `join_session` | `{player_name, character_id?}` | connect/reconnect; `character_id` present on rejoin |
| `player_action` | `{text, target?}` | the core "what do you do" free-text input |
| `chat_message` | `{text}` | out-of-character chat between players, not adjudicated |
| `character_edit` | `{field, value}` | player-side bookkeeping (inventory note, etc.) that doesn't need DM adjudication |
| `dice_roll` | `{dice: "1d20", reason}` | player-initiated roll (vs. DM-requested) |
| `reconnect` | `{player_id, last_event_id}` | resync log/state after a dropped connection |

## Server → Client

| type | payload | purpose |
|---|---|---|
| `state_sync` | `{characters[], npcs[], world_state, turn_order, current_turn, log_tail[]}` | full snapshot on join/reconnect |
| `log_entry` | `{kind: narration\|action\|dice\|chat\|system, text, chunk?, done?}` | append to the shared log pane; `chunk`/`done` support streaming DM narration token-by-token |
| `character_update` | `{player_id, sheet_delta}` | partial sheet push (HP change, item gained) — routed only to that player (and DM) |
| `npc_update` | `{name, sheet_delta}` | partial NPC/monster sheet push (HP change, condition) — broadcast to everyone, unlike `character_update`, since an NPC's wounds are shared observable fiction, not one player's private sheet |
| `world_update` | `{location, summary, flags, objectives[]}` | full `WorldState` push, broadcast to everyone, whenever the DM's `update_world` tool call actually changes something (`AnthropicNarrator` only for now — see ROADMAP.md item 6) |
| `turn_prompt` | `{player_id, prompt_text}` | whose turn it is, what's expected of them |
| `dice_result` | `{roller_id, dice, result, rolls, purpose, dc?, success?}` | outcome of any roll, DM- or player-initiated; `dc`/`success` present only for a DM-requested roll with a pass/fail threshold (`request_roll` tool, `AnthropicNarrator` only for now — see ROADMAP.md item 6) |
| `player_joined` / `player_left` | `{player_id, name}` | presence updates |
| `system_message` | `{level: info\|warning\|error, text}` | connection/errors, not part of the narrative |

## Turn arbitration: strict queue

Decided: strict turn queue, not free-for-all with DM-narrated simultaneity.

- Server holds `turn_order` (list of player ids) and `current_turn` (whose turn it is now).
- Only a `player_action` from the player matching `current_turn` is accepted and forwarded to the LLM for adjudication.
- A `player_action` from anyone else is rejected with a `system_message` (`level: "warning"`), and dropped — it does not queue up for later.
- `chat_message`, `dice_roll`, and `character_edit` are exempt from turn order — always allowed, since they don't touch adjudicated game state.
- On resolving an action: server applies state changes, emits `log_entry`/`character_update`/`dice_result` as needed, advances `current_turn` to the next player in `turn_order`, and broadcasts a new `turn_prompt`.

## Streaming narration

`log_entry` with `kind: "narration"` may fire repeatedly with partial `chunk` text and `done: false`, followed by a final message with `done: true`. This mirrors LLM token streaming and gives clients a "DM is typing" feel without a separate event type.

## Private vs. shared state

`character_update` payloads are routed only to the owning player's connection (plus the DM/server-side state). If other players should see the effect narratively (e.g. "Alice takes 4 damage"), that's a separate `log_entry` of `kind: "action"` broadcast to everyone, alongside the private `character_update`.

NPCs/monsters have no private owner, so `npc_update` is broadcast outright — no separate `log_entry` needed for other players to see it. The DM's `update_character` tool call now takes an optional `target` field: omitted (or `"self"`) means the acting character's own private sheet as before; a name means an NPC — the first call for a given name creates its tracked sheet server-side (in `Session.npcs`, alongside `Session.characters`), and every later call with that name updates the same tracked NPC, so wounds/conditions persist turn to turn instead of only ever existing in that turn's prose.

World state (`Session.world`) has no private owner either, same reasoning as NPCs — `world_update` is broadcast outright on any real change from the DM's `update_world` tool call (location, standing summary, objectives, flags).

## Implementation status

- **Implemented**: `join_session`, `player_action`, `chat_message`, `dice_roll` (client-side commands `/chat <text>` and `/roll <NdM[+/-K]> [reason]` in the Textual client's input bar), `state_sync`, `log_entry`, `turn_prompt`, `system_message`, `dice_result` (both player-initiated via `dice_roll` and, for `AnthropicNarrator` only so far, DM-initiated via the `request_roll` tool mid-turn — see ROADMAP.md item 6), `character_update` (pushed to the acting player whenever the DM's `update_character` tool call actually changes something — HP, inventory, conditions, or its `notes`), `npc_update` (broadcast to everyone whenever `update_character` targets a named NPC and something actually changed, including the NPC's own introduction), `world_update` (broadcast to everyone whenever the DM's `update_world` tool call actually changes something — `AnthropicNarrator` only for now).
- **Known client gap**: the client's `_handle()` has no branch for `dice_result` — a manual `/roll`'s outcome is only visible today via its accompanying `log_entry` (`kind: "dice"`) text line, not the structured envelope. Harmless for plain text display, but blocks any richer client-side rendering (e.g. highlighting a natural 20, a dedicated roll widget) until added.
- **Not yet implemented** (defined here, no server handler): `character_edit`, `reconnect` as a distinct event — today, reconnecting is just calling `join_session` again with the same `player_id`, which the engine already treats as resuming an existing character rather than creating a new one. A dedicated `reconnect` event may turn out to be unnecessary; revisit before building it.
- `player_joined`/`player_left` are defined but not yet emitted by the engine.

## Open questions

- Transport: WebSockets, finalized (`websockets.asyncio`).
- Reconnection semantics beyond identity resume (e.g. how long a disconnected player's turn is held before skipping) — relevant once multiplayer is exercised.
- Whether DM-initiated events (e.g. random encounters between turns) need their own trigger outside the player action cycle. Partially answered on one specific case: a genuine campaign start now triggers a DM-narrated opening scene via `GameEngine._narrate_opening_scene` (see ROADMAP.md item 6), reusing the normal turn machinery rather than a new event type. General background events *between* turns (not just at session start) remain unaddressed.
