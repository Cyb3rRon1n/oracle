# Oracle v2 Rebuild Plan

Rebuild of Oracle using [OpenArcana](https://github.com/krys64/OpenArcana) as the UX/design
reference. OpenArcana is client-trust single-player (model JSON mutates state directly,
localStorage saves); Oracle stays server-authoritative — OpenArcana's ideas are ported as
**server-side concepts**, not as its architecture.

## Locked decisions

| Decision | Choice |
|---|---|
| Stack | Python engine/server **stays**; new web client replaces the TUI |
| Client | React 18 + Vite + Tailwind, plain JS/JSX (no TypeScript) |
| Game logic | Server-authoritative: LLM narrates/adjudicates via tools; engine computes all mechanics |
| Multiplayer | Preserved — hotseat + networked, same WebSocket protocol |
| Old TUI | Deleted at cutover (phase 7) |
| AI providers | Ollama + Anthropic + one OpenAI-compatible client (Deepseek/Kimi/Grok/OpenAI) in phase 2 |
| Saves | Server persistence stays authoritative; no localStorage saves |

## What goes where

**Kept from Oracle:** `server/engine.py`, `server/state.py`, `server/dice.py`,
`server/rules/srd.json`, `server/lore/`, `server/persistence.py`, `server/transport.py`,
`shared/protocol.py` (extended).

**Ported from OpenArcana (adapted to server-authority):**

| OpenArcana | Oracle v2 |
|---|---|
| Model JSON mutates HP/gold/inventory directly | Same UX; mutations flow through DM tools → engine validates/computes |
| Emoji coordinate map | `update_world` gains structured map data (x/y, icon, name); server stores it, clients render |
| World Context file injection | Server-side loader (`world_context/` → toggled docs into DM prompt, size budgets); txt/md/json first, PDF later |
| Sliding window + rolling summary | Memory summarizer in narrator; re-run tool-reliability harness after (baseline context changes) |
| Multi-provider registry | One OpenAI-compatible client alongside Ollama/Anthropic |
| Dice roller UI, i18n FR/EN, medieval theme | Client-side; rolls execute server-side |

**Dropped:** Textual TUI (at cutover), localStorage saves.

## Phases

1. **Protocol design** — DONE 2026-08-23: schemas frozen in docs/protocol.md ("Protocol v2 additions").
2. **Server upgrades** — DONE 2026-08-23:
   - Map/clock state (`server/state.py`: `MapNode`, `Clock`, computed `map` snapshot,
     `apply_update` extensions incl. `map_nodes` upserts and clock args)
   - Lorebook (`server/lorebook.py`) + `context_manifest_request`/`context_select`/
     `context_manifest` handlers; per-turn keyword injection appended to `world_summary`
   - Rolling campaign summarizer (`Session.campaign_summary`, every
     `CAMPAIGN_SUMMARY_INTERVAL=10` turns, optional `summarize()` on both backends)
   - Provider registry: `DM_BACKEND=openai` via new `server/narrator_openai.py`
     (any /chat/completions endpoint - Deepseek/Kimi/Grok/OpenAI), decide-call
     validation-retry built in
   - Two-phase turns on Ollama (default ON; `OLLAMA_TWO_PHASE=0` keeps the legacy
     single-call path for A/B) and post-turn scene-fact extraction on Anthropic;
     `scene_update` broadcast assembled engine-side, suggested_actions capped at 4
     server-side
   - Tests: tests/test_protocol_v2.py (26); full suite green (two
     test_persistence unwritable-dir failures are root-user artifacts - uid 0 can
     write chmod-500 dirs - pre-existing, unrelated)
3. **Client scaffold** — DONE 2026-08-23: Vite+React+Tailwind app in `web/`, WS client with
   queue+backoff reconnect, one reducer per protocol event, join screen (shareable session id,
   class/race, character import), streaming log shell. Wire-verified against the live server.
4. **Game screen parity** — DONE 2026-08-23: tabbed character sheet
   (Overview/Abilities/Inventory/Spells/Features & Notes) on the owner-only payload; bookkeeping
   via `character_edit` (notes/add_item/remove_item/equip/unequip); death-save roll button when
   dying; confirmable-proposal banner (`apply_proposed_change`); scene panel (objectives, present
   NPCs, POI chips, suggested-action chips feeding the input, clock pips); client-side export of
   character .json + transcript .txt. Wire-verified against the live server.
5. **Map panel + dice roller** — DONE 2026-08-23: SVG map from the v2 snapshot (placed nodes at
   coords with bounds-fit scaling, unplaced on an auto ring, dashed edges, pulsing gold ring on
   the current location) as a sheet "Map" tab; dice tray (d4-d20) sending `dice_roll`, results via
   broadcast `dice_result` (log lines come from the engine's own `log_entry` - no client-side
   duplication). Wire-verified.
6. **i18n + theme polish + multiplayer live test** — DONE 2026-08-23: `web/src/i18n.jsx`
   (English strings as keys, French dict override, 🇫🇷/🇬🇧 header flags, localStorage
   persistence); vignette theme pass; multiplayer verified with two independent WS clients on
   one session: presence cross-visible, owner-view redaction holds (B sees none of A's
   inventory/stats/notes), chat broadcasts to both, turn order forms [A,B], out-of-turn action
   rejected.
7. **Cutover** — DONE 2026-08-23: TUI client deleted (`client/`,
   `test_client_app.py`, TUI screenshot/demo scripts, TUI capture SVGs);
   transport e2e coverage replaced by `tests/test_transport_ws.py` (real
   socket, no terminal dependency); pyproject drops textual + client package;
   CI gains a web-build job; README rewritten for v2; CONTRIBUTING notes the
   web build gate. Suite green: 536 passed (+2 root-user permission artifacts
   that only fail under uid 0).

## Research-derived upgrades (2026-08-23 web-research pass, folded into phases)

| Upgrade | Phase |
|---|---|
| Two-phase turn: constrained decide-call → unconstrained narrate-call | 2 |
| Constrained decoding for tool args (Ollama structured output) + Pydantic validation-retry | 2 |
| Keyword-triggered lorebook (SillyTavern World Info pattern) — replaces whole-file injection | 2 + protocol |
| Structured scene envelope (`scene_update`) assembled server-side | 1 (schema) + 4 (UI) |
| Progress/doom clocks as server state, ticked via `update_world` | 1 (schema) + 4 (UI) |
| Graph-of-locations map + layout hints (existing `location_map` extended) | 1 (schema) + 5 (UI) |
| Watchlist / later: fact ledger (AriGraph-lite), narrator+archivist agent split with per-agent model routing, proactive director ticks, TTS/scene-image sidecars | post-v2 |

Sources and full briefing: task-observer log, 2026-08-23 session. Key refs:
SillyTavern World Info docs; AriGraph (arXiv:2407.04363); Zep/Graphiti (arXiv:2501.13956);
agentic GM study (arXiv:2502.19519); Talemate; constrained-decoding surveys.

## Notes

- The tool-call reliability harness (`evidence/`, ROADMAP experiments) survives phase 2's
  narrator changes, but two-phase turns + summarizer change prompt context — re-run the
  harness after phase 2 so ROADMAP percentages stay honest.
- Desktop packaging: optional thin Electron wrapper later; not planned.
- Protocol v2 schemas frozen in docs/protocol.md ("Protocol v2 additions").
