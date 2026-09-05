# Changelog

Completed work, newest first. This is the history that used to live in
ROADMAP.md's "Current state" and "Next" sections; the reasoning behind each
item is in its commit message and the git history. ROADMAP.md now tracks
only open work.

## v2 rebuild and after (2026-08)

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
