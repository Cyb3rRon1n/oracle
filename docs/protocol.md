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
| `join_session` | `{player_name, character_class?, character_id?, imported_character?}` | connect/reconnect; `character_id` present on rejoin. `character_class` (one of `server/engine.py`'s `CLASS_STARTING_EQUIPMENT` keys - `fighter`/`wizard`/`rogue`/`cleric`) only matters on a genuinely new character: it sets starting HP from the SRD's `hit_die` and a starting item or two, instead of every character beginning blank. Ignored on reconnect (an existing character keeps what it already has); blank or unrecognized falls back to the original hp=10/max_hp=10 empty sheet. `imported_character` (a full `CharacterSheet` dict, from a previously exported file - see "Character export/import" below) is new-character-only too, and wins over `player_name`/`character_class` when present and valid. |
| `player_action` | `{text, target?}` | the core "what do you do" free-text input |
| `chat_message` | `{text}` | out-of-character chat between players, not adjudicated |
| `character_edit` | `{field, value}` | player-side bookkeeping (inventory note, etc.) that doesn't need DM adjudication |
| `dice_roll` | `{dice: "1d20", reason}` | player-initiated roll (vs. DM-requested) |
| `reconnect` | `{player_id, last_event_id}` | resync log/state after a dropped connection |
| `start_session` | `{}` | the pre-game lobby's "Start Adventure" trigger - any joined player may send this (no host/GM role exists). No-op if the adventure has already started (idempotent - see `session_started` below) |

## Server → Client

| type | payload | purpose |
|---|---|---|
| `state_sync` | `{characters[], npcs[], world_state, turn_order, current_turn, log_tail[], started}` | full snapshot on join/reconnect — the recipient's own `characters[]` entry is a full sheet, every other player's entry is the same redacted public view (below) `player_joined`/`player_update` broadcast. `started` (bool) tells the client whether to route to the pre-game lobby or straight to the real session view - see "Pre-game lobby and session start" below |
| `log_entry` | `{kind: narration\|action\|dice\|chat\|system, text, chunk?, done?}` | append to the shared log pane; `chunk`/`done` support streaming DM narration token-by-token |
| `character_update` | `{player_id, sheet_delta}` | full sheet push (HP change, item gained, inventory included, `xp`/`level`) — routed only to that player (and DM) |
| `player_update` | `{player_id, name, character_class, hp, max_hp, conditions, level}` | the public counterpart to `character_update` — broadcast to everyone whenever a player's own sheet changes, but redacted to what other players may see (no `inventory`, `stats`, `notes`, or `xp`) so every client's presence view of everyone else stays live |
| `npc_update` | `{name, sheet_delta}` | partial NPC/monster sheet push (HP change, condition) — broadcast to everyone, unlike `character_update`, since an NPC's wounds are shared observable fiction, not one player's private sheet |
| `world_update` | `{location, summary, flags, objectives[]}` | full `WorldState` push, broadcast to everyone, whenever the DM's `update_world` tool call actually changes something (`AnthropicNarrator` only for now — see ROADMAP.md item 6) |
| `turn_prompt` | `{player_id, prompt_text}` | whose turn it is, what's expected of them |
| `dice_result` | `{roller_id, dice, result, rolls, sides, purpose, dc?, success?}` | outcome of any roll, DM- or player-initiated; `dc`/`success` present only for a DM-requested roll with a pass/fail threshold (`request_roll` tool, `AnthropicNarrator` only for now — see ROADMAP.md item 6). `sides` is the die size actually rolled (from `server/dice.py`'s own notation parser) - lets a client tell a natural max/min individual roll apart from an ordinary one without re-parsing `dice` itself |
| `player_joined` | `{player_id, name, character_class, hp, max_hp, conditions, level}` | broadcast whenever a player joins or reconnects — the same public view as `player_update`, used by clients to add/refresh a presence line |
| `player_left` | `{player_id, name}` | broadcast whenever a connected player's socket closes, so clients can drop their presence line |
| `session_started` | `{}` | broadcast once, in response to `start_session` — tells every client still in the pre-game lobby to transition into the real session view. Fires *before* any opening-scene narration begins (see "Pre-game lobby and session start" below for why the ordering matters) |
| `system_message` | `{level: info\|warning\|error, text}` | connection/errors and turn-order rejections, not part of the narrative; also used as a best-effort, deliberately imperfect heuristic warning when narration reads like it resolved damage/a death/a condition but no `update_character` call actually fired that turn (see ROADMAP.md's tool-call reliability investigation) - a nudge that the sheet may be stale, not a claim that it definitely is; and, since `GameEngine._save()`'s save-failure handling, a `level: "warning"` sent to the relevant player whenever persisting session state fails (e.g. a vanished `SESSION_STORE_DIR`) - the join/start/turn that triggered it still completes normally, this only flags that the resulting state may not have actually been written to disk |

## Turn arbitration: strict queue

Decided: strict turn queue, not free-for-all with DM-narrated simultaneity.

- Server holds `turn_order` (list of player ids) and `current_turn` (whose turn it is now).
- Only a `player_action` from the player matching `current_turn` is accepted and forwarded to the LLM for adjudication.
- A `player_action` from anyone else is rejected with a `system_message` (`level: "warning"`), and dropped — it does not queue up for later.
- `chat_message`, `dice_roll`, and `character_edit` are exempt from turn order — always allowed, since they don't touch adjudicated game state.
- On resolving an action: server applies state changes, emits `log_entry`/`character_update`/`dice_result` as needed, advances `current_turn` to the next player in `turn_order`, and broadcasts a new `turn_prompt`.

## Pre-game lobby and session start

Owner's own framing (see ROADMAP.md): *"should there be a main menu first where players can join, chat, create or load their character and or review their character... then when start dm begins narrating the scene... maybe begin with players introducing themselves."* `join_session` still creates/resumes your character immediately - the lobby is about what happens (or, deliberately, doesn't happen) *after* that.

- **`join_session` no longer implicitly starts anything.** It creates/resumes the joining player's character and broadcasts `player_joined`/the "X joined" `system_message` as before, but narration and the turn queue stay dormant - no opening scene, no `turn_prompt` - until a separate `start_session` arrives. `chat_message` is exempt from turn order already, so lobby chat (`LobbyScreen`'s own chat input, distinct from `SessionScreen`'s `/chat` command) works with zero engine changes.
- **`start_session` is symmetric, not host-gated.** Any joined player may send it - Oracle has no separate host/GM role anywhere else in the protocol (turn order itself is symmetric too), so this doesn't introduce one. It's idempotent: once the adventure has started (`GameEngine._has_started()` - `Session.started` or, for backward compatibility with a session saved before that field existed, a non-empty `log`), a repeat `start_session` is a silent no-op.
- **A multi-player opening scene is a richer *prompt*, not a new tool-routing mechanism.** If more than one character has joined by the time `start_session` fires, the synthetic opening-scene action text lists everyone present by name/class and nudges the DM to acknowledge the group and invite introductions - `character_summary`/`apply_update` still anchor on a single character exactly like any other turn. The owner's own framing used "maybe" for the introductions detail, so this stays a prompt-level nudge, not a hard requirement enforced anywhere.
- **Ordering matters and is deliberate: `session_started` broadcasts *before* the opening scene's narration, not after.** `_narrate_and_apply` (inside `_narrate_opening_scene`) streams `log_entry`/`npc_update` broadcasts as narration happens - a client still on the lobby screen has no `#log`-equivalent widget to render them into. Broadcasting `session_started` first lets every client transition into the real session view first, then watch the opening scene stream in live - the same experience a normal turn's narration already gives, not a special case.
- **A failed or disabled opening scene still starts the session.** `_narrate_opening_scene`'s existing best-effort framing (a warning `system_message`, not a crash) is unchanged; `_on_start_session` commits to `Session.started = True` before attempting narration at all, so a failure (or `enable_opening_scene=False`, e.g. `scripts/live_reliability_check.py`'s own engine) doesn't leave the session stuck re-offering "Start" forever - the turn queue still becomes live either way.

## Streaming narration

`log_entry` with `kind: "narration"` may fire repeatedly with partial `chunk` text and `done: false`, followed by a final message with `done: true`. This mirrors LLM token streaming and gives clients a "DM is typing" feel without a separate event type.

## Private vs. shared state

`character_update` payloads are routed only to the owning player's connection (plus the DM/server-side state). If other players should see the effect narratively (e.g. "Alice takes 4 damage"), that's a separate `log_entry` of `kind: "action"` broadcast to everyone, alongside the private `character_update`.

**Other players do get a redacted view of each other, not silence.** `name`, `character_class`, `hp`/`max_hp`, `conditions`, and `level` are considered public — narratively visible to anyone in the scene — while `inventory`, `stats`, `notes`, and `xp` stay private to the owning player, mirroring `character_update`'s existing owner-only routing for the full sheet. This public subset is what `player_joined` (on join/reconnect), `player_update` (broadcast alongside every private `character_update`, so it stays live), and every non-owning entry in `state_sync`'s `characters[]` all carry — one shape (`GameEngine._public_character_view` server-side), used everywhere a player's presence needs to be visible to someone other than themselves. `level` is public the same way class or HP is (a real fact about the character visible at the table); raw `xp` stays owner-only bookkeeping, the number that actually determines *when* the next level lands rather than a fact about the character itself.

NPCs/monsters have no private owner, so `npc_update` is broadcast outright — no separate `log_entry` needed for other players to see it. The DM's `update_character` tool call now takes an optional `target` field: omitted (or `"self"`) means the acting character's own private sheet as before; a name means an NPC — the first call for a given name creates its tracked sheet server-side (in `Session.npcs`, alongside `Session.characters`), and every later call with that name updates the same tracked NPC, so wounds/conditions persist turn to turn instead of only ever existing in that turn's prose.

World state (`Session.world`) has no private owner either, same reasoning as NPCs — `world_update` is broadcast outright on any real change from the DM's `update_world` tool call (location, standing summary, objectives, flags).

## Character progression: XP and leveling

The primary mechanical foundation this project is being built around (per the owner's own framing: "the primary end result is having strong mechanics for Oracle") — D&D-style XP and leveling, deliberately **deterministic**, not dependent on the DM model reliably calling a dedicated tool.

- **Trigger: an NPC's HP crossing from >0 to 0, observed by the engine itself.** `GameEngine`'s `apply_update` closure (`server/engine.py`) already applies every `update_character` call against a tracked `Session.npcs` entry — awarding XP off that same, already-reliable state transition means it can't silently fail the way a separate "award_xp" tool call could. ROADMAP.md's own tool-call reliability investigation found local-model tool-call reliability plateaus around 29% across every model tested; a mechanic that depended on the model remembering to call a second tool on top of the damage update would inherit that same failure rate. An already-dead NPC (HP already 0 entering the update) never re-triggers an award, however many more times it's hit afterward.
- **XP value, in priority order:** (1) an explicit `xp` on the killing `update_character` call — the same override precedent `max_hp` already has when introducing an NPC, lets the DM hand-tune a boss or a trivial mook; (2) the NPC's name matched against `server/rules/srd.json`'s own monster list (the same lookup `lookup_rule` already uses) and its Challenge Rating run through the SRD's real Experience Points by Challenge Rating table (`server/rules/srd.json`'s new `leveling.xp_by_cr`) — free and automatic whenever an NPC happens to be named after a known monster; (3) a flat default (`DEFAULT_NPC_XP`, mirroring `DEFAULT_NPC_HP`'s existing role as a safety net, not the intended path).
- **XP recipient: whoever's turn it is, not the whole party.** A deliberate simplifying default, not an oversight — real 5e splits XP across the whole party, but Oracle's turn loop already anchors every mechanical update (`apply_update`/`request_roll`/`update_world`) on a single acting character with no session-wide "who else is present and should share credit" notion. Worth revisiting if/when a real multi-character-per-turn scenario shows up.
- **Level thresholds: the SRD's real Character Advancement table** (`server/rules/srd.json`'s new `leveling.xp_by_level`, levels 1-20), not an invented curve — `CharacterSheet.gain_xp()` (`server/state.py`) loops rather than checking the next threshold once, so a single large award can cross more than one level at a time.
- **HP growth on level-up: the class's hit die maximum per level gained**, the same no-ability-scores simplification `build_starting_character`'s own level-1 HP already uses (real 5e adds a CON modifier and, past level 1, typically rolls instead of taking the max — both deliberately out of scope until ability scores exist at all). A character with no recognized class (blank `character_class`) still levels, just without HP growth — a real, documented gap rather than a fabricated number.
- **Announcement: a broadcast `system_message`**, not a new envelope type — `"{name} defeats {npc} and gains {xp} XP!"`, with `"{name} reaches level {N}!"` appended when a level-up happened. Matches how every other game-flow event that isn't itself DM narration (a join, an out-of-turn rejection) already reaches clients, so no protocol/client change was needed just to render it.

## Character export/import

The secondary half of the owner's XP/leveling request: *"if possible character saving format so players can import or export their character as an extra layer to not lose their progression."* Scoped to a single `CharacterSheet` snapshot — distinct from a full session-transcript export (a separate, already-tracked item), and deliberately not a new protocol envelope type for either direction.

- **Export is client-side only, no protocol change.** `DungeonMasterApp.export_character()` (`client/app.py`) writes `self.my_character` — already the exact private full-sheet dict `state_sync`/`character_update` deliver to its owner, xp/level/inventory/notes included — straight to a local JSON file. Triggered via a new `/export [filename]` command, available both in `SessionScreen`'s input bar (alongside `/roll`/`/chat`) and `LobbyScreen`'s chat input (reviewing/saving a character before the adventure starts is exactly the moment the owner's own original lobby framing described).
- **Import is a `join_session` field, new-character-only.** A new optional `imported_character` payload field (a full `CharacterSheet` dict, read from a local export file client-side) — gated the same way `character_class` already is: it only matters when `_on_join_session` sees a genuinely new character, and is silently ignored on a reconnect (an existing character keeps what it already has, never overwritten by a stale import). When present and valid, it wins over `player_name`/`character_class` entirely — the imported sheet's own `name`/`character_class`/`stats`/`inventory`/`xp`/`level`/`notes` are what gets used, not a merge with freshly-typed values. `player_id` is always overridden to the real joining connection's id, never trusted from the file itself.
- **Two independent validation layers, not one.** `client/app.py`'s `_load_character_file()` does the cheap client-side check (can the path be read, is it valid JSON, is it even an object) before anything is sent over the wire, so a bad path fails fast with a clear message in the welcome screen. `server/engine.py`'s `_character_from_import()` re-validates the full shape via `CharacterSheet(**imported)` regardless — a client is never a trusted boundary for another connection's data, so the real validation is server-side. Either layer failing falls back to the exact same fresh `build_starting_character()` sheet a blank/unrecognized `character_class` already falls back to, plus a `system_message` warning telling the player the import didn't take.

## Implementation status

- **Implemented**: `join_session`, `player_action`, `chat_message`, `dice_roll` (client-side commands `/chat <text>` and `/roll <NdM[+/-K]> [reason]` in the Textual client's input bar), `start_session`/`session_started` (the pre-game lobby's "Start Adventure" trigger and its lifecycle-transition broadcast - see "Pre-game lobby and session start" above), `state_sync`, `log_entry`, `turn_prompt`, `system_message`, `dice_result` (both player-initiated via `dice_roll` and, for `AnthropicNarrator` only so far, DM-initiated via the `request_roll` tool mid-turn — see ROADMAP.md item 6), `character_update` (pushed to the acting player whenever the DM's `update_character` tool call actually changes something — HP, inventory, conditions, or its `notes`), `player_joined`/`player_left`/`player_update` (broadcast on join/reconnect, on socket close, and alongside every `character_update` respectively — see "Private vs. shared state" above for what's actually in the public payload; the Textual client renders every other connected player as a status line in the same left-column sheet panel as your own sheet), `npc_update` (broadcast to everyone whenever `update_character` targets a named NPC and something actually changed, including the NPC's own introduction), `world_update` (broadcast to everyone whenever the DM's `update_world` tool call actually changes something — `AnthropicNarrator` only for now).
- **`dice_result` client rendering, closing the "Known client gap" this bullet used to describe.** The client now renders every roll from the structured `dice_result` envelope, not the accompanying plain-text `log_entry` (`kind: "dice"`, still broadcast server-side but skipped by the client to avoid showing each roll twice) — highlighting a natural max or min on any individual die (green/red, generalized beyond just a natural 20 on a d20) the plain text alone couldn't carry.
- **XP/leveling, implemented** — see "Character progression: XP and leveling" above. `CharacterSheet` gained `xp`/`level` fields (defaulting to `0`/`1`), awarded deterministically off NPC-defeat state transitions rather than a DM tool call.
- **Character export/import, implemented** — see "Character export/import" above. `/export [filename]` (client-side, `SessionScreen`/`LobbyScreen`), `join_session`'s new optional `imported_character` field (server-side, new-character-only, `_character_from_import()`).
- **Not yet implemented** (defined here, no server handler): `character_edit`, `reconnect` as a distinct event — today, reconnecting is just calling `join_session` again with the same `player_id`, which the engine already treats as resuming an existing character rather than creating a new one. A dedicated `reconnect` event may turn out to be unnecessary; revisit before building it.

## Open questions

- Transport: WebSockets, finalized (`websockets.asyncio`).
- Reconnection semantics beyond identity resume (e.g. how long a disconnected player's turn is held before skipping) — relevant once multiplayer is exercised.
- Whether DM-initiated events (e.g. random encounters between turns) need their own trigger outside the player action cycle. Partially answered on one specific case: a genuine campaign start now triggers a DM-narrated opening scene via `GameEngine._narrate_opening_scene` (see ROADMAP.md item 6), reusing the normal turn machinery rather than a new event type. General background events *between* turns (not just at session start) remain unaddressed.
