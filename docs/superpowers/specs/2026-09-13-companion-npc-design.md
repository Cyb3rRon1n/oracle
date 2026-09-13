# Companion NPC: "Tinder" — design

## Goal

An optional, DM-controlled party companion with a comic-relief personality,
addable/dismissable by the players, that (a) makes sessions livelier and
gives Oracle a reason to keep playing, and (b) gives the project a real
character to dogfood live sessions against — not a new automated scoring
harness, a genuine playtest.

## What already exists (reused, not rebuilt)

Oracle already has almost everything this needs:

- `CharacterSheet.personality` / `.ideals` / `.bonds` / `.flaws`
  (server/state.py:148-151) — free text, already read by the DM via
  `character_summary` on every turn, already player-editable post-creation
  via `character_edit`.
- Every character — PC *or* NPC — already gets these randomized at creation
  from a curated table (`random_origin`, server/lore/__init__.py) via
  `build_starting_character` (server/character_build.py). "Roulette
  uniqueness" is already the default behavior for every character today.
- `Session.npcs: dict[str, CharacterSheet]` (server/state.py:831) — NPCs use
  the *exact same* `CharacterSheet` model as players, including
  `range_band`, `disposition`, everything the BG3-backlog work added this
  session. A companion doesn't need a new type.
- `_public_character_view` — the existing mechanism that already redacts
  what one player can't see of another's sheet, and what any player can see
  of an NPC. Companion visibility parity with a real teammate falls out of
  this for free.

So this spec is much smaller than "build a companion system" — it's
"surface a currently-invisible feature, and add one preset character that
leans on it."

## Piece A: creation-time personality control (all characters)

Currently `JoinScreen.jsx` only exposes name/class/race. Personality/
ideals/bonds/flaws are rolled silently server-side and only discoverable
after joining, via the sheet's edit fields. This piece:

- Surfaces the rolled origin (personality/ideals/bonds/flaws) in the join
  flow before the player confirms.
- Adds a "reroll" action (calls `random_origin` again) — keeps the existing
  randomness as the low-effort default path.
- Makes the four fields editable inline at that point, for players who want
  to hand-pick instead of rolling.

This is a general engagement improvement independent of the companion: it
makes the roleplay hook every character already carries actually visible
and ownable at the moment character investment starts, instead of being
discovered by accident later (or never).

## Piece B: the companion

### Identity

An original character (not a reskin of any copyrighted comic-relief
archetype — same IP-carefulness this project already applies to SRD
content) whose personality is *motivated by Aetherfall's own central
mechanic*, not bolted on:

Aetherfall's Veil takes something real from everyone who crosses it — a
memory, a name, a piece of who they were — chosen by the Veil, never by the
person. **Tinder's toll was their sense of fear/self-preservation.** They
don't have a death wish; gravity (the emotional kind) just doesn't apply to
them the way it used to, and jokes are what's left where caution used to
live. Underneath the routine there's a real, specific loss — matching the
world bible's own "wonder and real danger in equal measure, even in a
lighthearted scene" tone guidance, rather than undercutting it.

### Tone vs. the world bible

`server/lore/isekai.json`'s `tone_guidance` is explicit: *"Grounded and
character-driven, not zany or played for comedy."* Recommendation: **leave
it untouched.** Tinder is a deliberate contrast character — comic relief
reads precisely because the world around it stays serious. Changing the
ambient tone to accommodate one character would flatten everything else
the tone guidance protects. This is a conscious choice, not a silent one.

### Mechanics (or deliberate lack of them)

- Backed by a real `CharacterSheet` living in `session.npcs`, not a new
  type.
- **No HP, no combat participation, no action economy, no turn, no
  `player_action`.** Never enters initiative. This is the biggest scope cut
  in this spec, and it's what keeps engine surface near zero — no new
  protocol messages for "Tinder's turn," no interaction with the
  action-economy/range-band systems the last backlog arc built.
- **Reactive banter**: the DM's existing `narrate()` call already reads
  every present character's personality via `character_summary` /
  `world_summary` context. Tinder's presence is a system-prompt addition
  telling the DM to weave in in-character reactions when present — no new
  code path, a prompt change.
- **Rare "chaos beat"** (the piece you specifically asked for: Tinder
  occasionally does something that forces the party to adapt): DM-judgment,
  expressed as ordinary narration plus the DM's *existing* tools (NPC
  disposition, `update_world`, etc.) — never a new tool call. Deliberately
  not scheduled, cooldown-timed, or config-driven at this stage; tuned by
  feel during the live playtest (Testing plan below) rather than guessed at
  now. Ponytail: no config for a value we don't yet know needs tuning.
- **Pacing nudge**: a lightweight "turns since the world last meaningfully
  changed" counter (same cadence concept this session's
  `live_world_reliability_check.py --long` work already measures) surfaced
  as a hint in the DM's prompt context past some threshold — lets the DM
  (using Tinder if present, or just narration generally) notice stalled
  pacing and nudge it. Small, mostly a prompt-context addition; the counter
  itself is new state but tiny (one int, not a subsystem).

### Party membership & visibility

- Join/dismiss toggle, same shape as the ⚔ start/end combat toggle already
  shipped this session (`GameScreen.jsx` header control + a `store.jsx`
  action pair sending a small event).
- Once joined, other players see Tinder exactly as they'd see a real
  teammate — whatever `_public_character_view` already exposes for a party
  member, nothing more. No special "companion sheet" peek.
- Scoped to **one** companion, not a roster of selectable personas. Revisit
  only if this lands well in play.

## Piece C (separate, independent): free image-gen backend for testing

Not part of the companion feature — tracked here only because it surfaced
in the same conversation. Research (see CHANGELOG once logged) recommends
**Pollinations.ai**: free API key, no expiring credit pool (unlike
Stability AI/Replicate's one-time trial credits), 1 req/hr rate limit on
free keys (fine for occasional spot-checks). Wire in as a small
`PollinationsImageBackend` alongside the existing `ComfyUIBackend`/
`OpenAIImageBackend` (server/image_backend.py), reusing the already-generic
`IMAGE_API_KEY` env var rather than adding a Pollinations-specific one.
Small bounded change, not part of this spec's implementation plan.

## Testing plan

Once built, a real live session — not a new scored script — with Tinder
present, played interactively (or via a driven scripted walkthrough)
against a live backend, reported back as qualitative playtest notes: does
the banter feel stale, does the chaos beat land or annoy, does the pacing
nudge actually get used, does dismiss/rejoin work cleanly mid-session.

## Explicitly not doing (this spec)

- No mechanical/combat presence for Tinder (see above).
- No multi-persona companion roster.
- No change to `tone_guidance` or the world bible.
- No new automated reliability-script persona for Tinder (that was the
  *rejected* interpretation of "testing" — a real played session is what
  was asked for).
- Piece C (Pollinations backend) is not part of this implementation plan.

## Open questions for the implementation plan

- Exact new client→server event name(s) for join/dismiss.
- Exact field/flag distinguishing "Tinder, currently with the party" from
  an ordinary encountered NPC in `session.npcs` (a bool on the sheet vs. a
  session-level pointer).
- Exact pacing-nudge threshold (turns) and where the counter lives on
  `Session`.
- Full personality/ideals/bonds/flaws text for Tinder's preset sheet.
