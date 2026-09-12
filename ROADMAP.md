# Roadmap

Open work only. Completed work is in [CHANGELOG.md](CHANGELOG.md); the
reasoning behind any shipped decision is in its commit message.

**If you're an AI assistant reading this cold:** read this file plus
`docs/protocol.md` before making architectural changes. Several "obvious"
simplifications were deliberately rejected — e.g. "just always call
`update_character`" (measured ~29% reliable on local models — see CHANGELOG's
structured-output finding) and "just use full history" (unbounded cost). The
project is server-authoritative: the LLM narrates and adjudicates via tools,
the engine computes every mechanical number.

## What exists

A working AI-DM: Python engine/server + React web client over a websocket,
swappable narrator backend (Anthropic / Ollama / OpenAI-compatible). The
engine owns all state and 5e mechanics (HP, AC, XP, initiative, death saves,
spell slots, conditions, disadvantage). Persistent world/quest state, NPC
memory, a keyword-triggered lorebook, a rolling campaign summary, a fact
ledger. A between-adventures tavern lobby (roster, chat, ready-check,
in-tavern rest, an AI tavern-keeper, a quest board) with a real
adventure → tavern → next-adventure loop (`end_adventure`). A reproducible
tool-call reliability harness. Full list: CHANGELOG.md.

## Open

### Reliability

- **Verify the hosted Claude / OpenAI paths live.** Both share the identical
  engine/tool-loop code path but have only ever been tested against mocks —
  blocked on credentials (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env`).
  One command each: `python -m scripts.live_reliability_check --backend
  anthropic --out evidence/postv2/claude_sonnet5_combat_r1.json` (and
  `--backend openai`).
- **Re-measure after any prompt-context change.** Two-phase turns, the
  summarizer, the fact ledger all change what the model sees —
  `scripts/live_reliability_check.py --repeat 5` keeps the CHANGELOG
  percentages honest. The `num_ctx` pin has now been measured (74% pooled,
  qwen2.5:7b combat `--repeat 5`, up from ~66% — see CHANGELOG); a real,
  reproducible gain, though not the outsized move this note used to speculate.
- **Long-session memory frequency.** The tool-call side is solved
  (`update_world` measured 100% on `qwen3:8b`); the open question is whether
  the DM actually updates `summary` often enough across 20+ turn sessions, and
  whether `OLLAMA_WORLD_UPDATES` can default on.

### Features — later, lower priority

- **Character portrait generation is shipped** — the `ImageBackend` swappable-backend
  pattern this bullet used to only sketch is real (`server/image_backend.py`), backed by
  ComfyUI rather than the originally-bookmarked `ultra-fast-image-gen` (cross-platform,
  not Apple-Silicon-only — the right fit for a Docker-deployable project with Anvil
  already generating a ComfyUI stack). See CHANGELOG.md and `docs/protocol.md`'s
  "Portrait generation" section. A hosted alternative for GPU-less users is also shipped
  (`IMAGE_BACKEND=openai`, `OpenAIImageBackend`) — an explicit selector alongside
  `ComfyUIBackend`, same pattern as `DM_BACKEND`. **Still open**: scene generation off
  narration beats (prompt-building off `WorldState.mood` + a protocol envelope — the
  portrait half's `ImageBackend`/prompt-building pattern extends to this directly, not a
  redesign), and real GPU verification of the ComfyUI workflow (LoRA/hi-res-fix/
  face-detail) — this project currently has no GPU hardware to run one against; only
  unit-tested against a fake client/websocket so far.
- **Text-to-speech** for DM narration — likely the same GPU dependency, though
  TTS models are lighter; measure on CPU before assuming.
- **Background world-ticks** — NPCs pursuing goals, world state advancing
  between turns rather than staying frozen until poked. Deferred in favor of
  the smaller `update_world`/quest-log slice already built; adds more
  tool-calling surface, so revisit once reliability is in a better place.
- **A relationship graph** (who-knows-whom, typed edges, source-anchored) —
  the closest OSS sibling (`open-tabletop-gm`) leans on this for cheap
  continuity. Bigger and more speculative than the NPC `notes` field built;
  revisit if `notes` turns out not to be enough in play.
- **Further SRD monster/spell coverage** beyond the batches shipped — real
  content work, not a hard limit.
- **Weapon/armor proficiency gating** — Oracle lists class proficiencies on
  the sheet but nothing checks them (an unproficient attack still gets the
  proficiency bonus). The last real mechanical gap from
  `docs/character-sheet-gaps.md`; small, needs a prompt/engine tweak to the
  attack path. Everything else in that doc is shipped.

## Explicitly not doing

- Scraping or bundling copyrighted D&D sourcebook content (Monster Manual,
  Player's Handbook, DMG, published adventures) — not legally redistributable
  even in a private repo. See `server/rules/ATTRIBUTION.md` for what's included.
- Making the repo public on any automatic trigger — the owner's call.

## Notes

- **GPU:** migration to a CUDA box (RTX 2080, 8GB) is done and verified —
  ~20–40x faster per turn once the model is loaded (1.5–2s warm vs 30–90s on
  CPU). This is what makes `--repeat` studies and larger models practical.
- Desktop packaging (thin Electron wrapper): possible later, not planned.
