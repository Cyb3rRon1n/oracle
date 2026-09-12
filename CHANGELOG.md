# Changelog

Completed work, newest first. This is the history that used to live in
ROADMAP.md's "Current state" and "Next" sections; the reasoning behind each
item is in its commit message and the git history. ROADMAP.md now tracks
only open work.

## v2 rebuild and after (2026-08)

- **Portrait generation: hosted-API backend, a modular ComfyUI workflow, style picker, and real progress.** `IMAGE_BACKEND` now selects between `comfyui` (local, free) and `openai` (hosted, `gpt-image-1` — the GPU-less-user alternative this project's own ROADMAP had named as still open) — an explicit selector, not inferred from which URL happens to be set, mirroring `DM_BACKEND` exactly. `ComfyUIBackend`'s workflow is built from composable stages instead of one fixed graph: optional LoRA and hi-res-fix passes (both core ComfyUI nodes, always available, opt-in via `COMFYUI_LORA`/`COMFYUI_HIRES_FIX`) and an optional face-detail pass (the Impact Pack's `FaceDetailer` — a real custom-node dependency, so it's real-detected via `/object_info` and silently skipped when a bare install doesn't have it, never assumed). Generation progress now streams over ComfyUI's own WebSocket in real time (replacing the earlier `/history` polling loop entirely) and relays to the requesting player as a new `portrait_progress` event; the avatar's "Generating…" state is now a real percentage bar. Players can also pick an art style (fantasy/anime/comic/realistic — prompt-text tags, not LoRA swaps, so every option works with just a base checkpoint) before generating. None of this has been run against real GPU hardware yet — verified via unit tests against a fake ComfyUI client/websocket, not a live generation. See `docs/protocol.md`'s "Portrait generation" section. (2026-09-12)
- **Portrait generation, ComfyUI-backed.** A player-initiated "Generate portrait" button (character sheet, top-left next to the name) sends a new `generate_portrait` event; the server builds a prompt from the character's own class/race/background (`server/portrait.py`, mirrors nightwire's `_ROLE_VISUALS`-style tables) and calls a new pluggable `ImageBackend` (`server/image_backend.py`) — `ComfyUIBackend` by default, `None`/disabled when `COMFYUI_URL` is unset. No HTTP server or file storage added: the result is a base64 data URL stored directly on `CharacterSheet.portrait`, public like name/HP, riding the same WebSocket transport every other sheet field already uses — a deliberate departure from nightwire's disk-file-plus-static-route design (oracle has no HTTP layer at all), not a partial port. Text-prompt only for v1, no reference photos. See `docs/protocol.md`'s "Portrait generation" section. (2026-09-12)
- **Inventory rework: Equipped/Inventory split, real potion use, no more player self-add.** The Equipment section's free-text "add an item" box is gone — inventory only ever grows from the DM's own `update_character add_item`, never a player typing an item into existence. `character_edit` gains `use_item` (real dice roll, real HP heal through the same `apply_update` path a DM heal uses, consumes the item — currently just Potion of Healing, `server/rolls.py`'s `CONSUMABLE_EFFECTS`) and loses `add_item`. The sheet now shows a compact "Equipped" loadout above the full item list; per-item buttons (`equip`/`use`/`drop`) are server-computed (`equippable`/`usable` on each item) rather than always shown. One shared `InventoryPanel.jsx` replaces two near-identical copies in `CharacterSheetFull.jsx` and `CharacterSheet.jsx`. See `docs/protocol.md`'s "Consumable items" section. (2026-09-12)
- **Quest board** — between adventures the DM posts 2-4 "what next" hooks (optional `NarratorBackend.quest_hooks`; constrained JSON on Ollama), the party votes (`request_quests` / `vote_quest` / `quest_board`), and a plurality winner frames the next adventure's opening scene. Best-effort — a generation failure just shows "the road is open". Completes lobby Phase 2. (2026-09-06)
- **Return to the tavern between adventures** — `end_adventure` / `session_ended`: any player can wrap the current adventure and take the party back to the `LobbyScreen` (recap card, fresh ready-check). `campaign_summary` is refreshed and `history` cleared; the next start narrates a "new adventure begins" opening (`WorldBible.next_adventure_prompt`). `_has_started()` is now a plain flag (`Session.adventures_completed` disambiguates the legacy migration). Unblocks the quest board. (2026-09-06)
- **Tavern-keeper NPC** — the lobby gains an AI-voiced greeter (optional `NarratorBackend.tavern_line`; `log_entry` kind `keeper`), hard rate-limited to one line per 20s, silent once the adventure starts. Keeper name/persona from `WorldBible.tavern_keeper`. (2026-09-06)
- **`num_ctx` pin measured** — pinning Ollama's context window to 8192 (up from its silent 4096 default, which truncated the per-turn prompt from the front) took combat tool-call correctness to **74% pooled** (26/35, `--repeat 5`, qwen2.5:7b, two-phase), up from ~66% with structured output alone and ~29% native. Reproducible (4/5 runs ≥71%), 0 pseudo-tool-call leaks. A real gain, not the outsized move the ROADMAP had speculated. (2026-09-06)
- **`update_world` reliability** measured live on `qwen3:8b` — 10/10 across 2 repeats (was 4/12). (2026-08-25)
- **Fact ledger** (AriGraph-lite): durable per-session facts captured with zero extra LLM calls. (2026-08-24)
- **World-bible deepening** — Aetherfall gains layered history, factions, peoples, naming rules, a real map; five optional depth fields on `WorldBible`. (2026-08-23)
- **Fourth SRD content batch** — 4 monsters, 3 spells, real revival support. (2026-08-23)
- **No-call precision** — two decide-prompt variants tried and reverted; prompt wording can't buy restraint without costing recall. (2026-08-23)
- **Post-v2 harness re-run** — two-phase turns measured; a silent `.replace()` no-op in the decide prompts found and fixed (import-time assert added). (2026-08-23)
- **v2 rebuild** — React/Vite/Tailwind web client replaces the Textual TUI; protocol v2 (`context_manifest*`, `scene_update`); two-phase Ollama turns; OpenAI-compatible backend; keyword-triggered lorebook; rolling campaign summarizer; map/clock world state. TUI deleted at cutover. (2026-08-23)
- **In-combat indicator + initiative-order display** — survives reconnect/scroll. (2026-08-22)
- **Third SRD content batch** — 4 monsters, 3 spells. (2026-08-22)
- **`WorldState.summary` reaches the DM's per-turn context**, not just a reconnecting player's. (2026-08-22)
- **`check_missed_change()` un-gated** from the regex — a second, independent detection channel. (2026-08-22)
- **`HARDENED_RULES_ADDENDUM` A/B** — live-run, null result, stays opt-in. (2026-08-22)
- **`--scenario persuasion`** — harness measures rule-adherence under rhetorical pressure. (2026-08-21)
- **DM-facing tracked-NPC roster** — dispositions/notes/wounds survive the history window. (2026-08-21)
- **Second SRD content batch** — 6 monsters, 5 spells. (2026-08-21)
- **Class progressions beyond level 1** — real per-level features, shown as levels are gained. (2026-08-21)
- **Confirmable missed-change correction** — the advisory carries an `/apply`-able proposal. (2026-08-19)
- **Party-wide XP splitting**. (2026-08-19)
- **Presentation polish** — dark-dungeon theme, README wordmark, animated-demo recorder. (2026-08-19)
- **`WorldState` scene-mood tag** — a hook for future image generation. (2026-08-19)
- **Long-gap reconnect recap** — grounds the DM's own context, not just the player's. (2026-08-12)
- **Missed-change self-correction** — the DM gets a real chance to fix its narration, not just a warning. (2026-08-12)
- **Unified the two dice-log label builders** — a real drift bug fixed. (2026-08-11)
- **Race subraces** — High/Wood Elf, Hill/Mountain Dwarf, Lightfoot/Stout Halfling. (2026-08-11)
- **Structured inventory items** — real quantities and magic bonuses, not a flat name list. (2026-08-11)
- **Race system** — race field + racial-trait data + ability bonuses. (2026-08-11)
- **Reliability harness** promoted to `scripts/live_reliability_check.py` — fixed 8-turn scenario through the real engine, JSON reports, `--repeat`. (2026-08-09→)
- **SRD content batch** — 5 monsters, 4 spells, 4 equipment. (2026-08-09)
- **Critical hits + colour-coded combat outcome log**. (2026-08-09)
- **Equipped gear** — weapon/armor/shield slots distinct from carried inventory; real computed AC. (2026-08-09)
- **Tabbed character sheet UI**. (2026-08-09)

### Structured-output finding (2026-08, the reliability investigation)

Every native-tool-calling local model tested (`qwen2.5:7b/14b`, `llama3.1:8b`,
`qwen3:8b`, `llama3-groq-tool-use:8b`) plateaued ~29% on the acting tool call,
non-reproducibly, regardless of scale, native tool-use training, prompt
reminders, or history-window size. **Switching to constrained JSON output
roughly doubled it (66% vs 29% pooled, `qwen2.5:7b`, `--repeat 5`)** — changing
the response *mechanism*, not the model. Now `OllamaNarrator`'s default
(`OLLAMA_STRUCTURED_OUTPUT=0` escapes). Scale did not help: a 32B lost to the
7B on the identical scenario. The visible missed-change advisory + `/apply`
proposal stay as a safety net decoupling player trust from model reliability.

## Foundational features

Death saves · ability scores driving HP growth and roll modifiers · ASIs at
SRD levels · per-class skill + saving-throw proficiencies · proficiency-driven
spell save DC · formal DEX-modified initiative · wizard/cleric spellcasting
with real slot tracking · rest/recovery (engine computes the heal) · mechanical
conditions imposing disadvantage · deterministic XP off engine-observed HP=0,
party-split, real SRD Character Advancement thresholds · character export/import
(`/export`) · session-transcript export (`/transcript`) · player-side
`character_edit` (notes/inventory) · NPC/monster state tracking with a shared
`CharacterSheet` model · persistent `WorldState` (location, summary, objectives,
flags, map) via `update_world` · NPC memory + disposition · DM-generated opening
scene · rolling 6-turn history window · deterministic setting facts
(`server/lore/`) · SRD lookup tool + hosted `web_search` (Anthropic) · swappable
`NarratorBackend` and `SessionStore` · one server process, many concurrent
sessions · startup writability check + visible save-failure handling.
