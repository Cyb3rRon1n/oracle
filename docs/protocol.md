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
| `state_sync` | `{characters[], world_state, turn_order, current_turn, log_tail[]}` | full snapshot on join/reconnect |
| `log_entry` | `{kind: narration\|action\|dice\|chat\|system, text, chunk?, done?}` | append to the shared log pane; `chunk`/`done` support streaming DM narration token-by-token |
| `character_update` | `{player_id, sheet_delta}` | partial sheet push (HP change, item gained) — routed only to that player (and DM) |
| `turn_prompt` | `{player_id, prompt_text}` | whose turn it is, what's expected of them |
| `dice_result` | `{roller_id, dice, result, purpose}` | outcome of any roll, DM- or player-initiated |
| `player_joined` / `player_left` | `{player_id, name}` | presence updates |
| `system_message` | `{level: info\|warning\|error, text}` | connection/errors, not part of the narrative |

## Turn arbitration: strict queue

Decided: strict turn queue, not free-for-all with DM-narrated simultaneity.

- Server holds `turn_order` (list of player ids) and `current_turn` (whose turn it is now).
- Only a `player_action` from the player matching `current_turn` is accepted and forwarded to the LLM for adjudication.
- A `player_action` from anyone else is rejected with a `system_message` (`level: "warning"`), and dropped — it does not queue up for later.
- `chat_message` and `character_edit` are exempt from turn order — always allowed, since they don't touch adjudicated game state.
- On resolving an action: server applies state changes, emits `log_entry`/`character_update`/`dice_result` as needed, advances `current_turn` to the next player in `turn_order`, and broadcasts a new `turn_prompt`.

## Streaming narration

`log_entry` with `kind: "narration"` may fire repeatedly with partial `chunk` text and `done: false`, followed by a final message with `done: true`. This mirrors LLM token streaming and gives clients a "DM is typing" feel without a separate event type.

## Private vs. shared state

`character_update` payloads are routed only to the owning player's connection (plus the DM/server-side state). If other players should see the effect narratively (e.g. "Alice takes 4 damage"), that's a separate `log_entry` of `kind: "action"` broadcast to everyone, alongside the private `character_update`.

## Open questions

- Transport: leaning WebSockets (JSON envelopes over `websockets` or Textual's async support) — not yet finalized.
- Reconnection semantics beyond `last_event_id` resync (e.g. how long a disconnected player's turn is held before skipping).
- Whether DM-initiated events (e.g. random encounters between turns) need their own trigger outside the player action cycle.
