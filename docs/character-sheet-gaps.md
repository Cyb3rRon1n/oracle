# Character sheet — what Oracle tracks vs. the official 5e sheet

The full character sheet overlay (`web/src/components/CharacterSheetFull.jsx`) is laid
out like the classic WotC 5e sheet. Sections Oracle actually tracks are populated;
sections it doesn't are rendered greyed with a `planned` tag, so the sheet doubles as a
build list.

This file is that list — every official-sheet field Oracle has no data for, what the
paper sheet does with it, and a rough cost to add. Ordered roughly cheapest-first.

## Trivial (client-only or one server line)

| Field | Paper sheet | Add it |
|---|---|---|
| **Full 18-skill block** | Every skill listed with its ability + a proficiency dot + total | Done in the overlay: fixed skill→ability map in `web/src/lib/dnd5e.js`, total = ability mod + (proficient ? prof bonus : 0). Server already sends `skill_proficiencies`. |
| **Saving throw proficiencies** | Two marked saves per class | Done: `saving_throw_proficiencies` added to `_owner_character_view` (constant `CLASS_SAVING_THROW_PROFICIENCIES` already existed). |
| **Spell attack bonus** | prof + spellcasting mod | Done: `spell_attack_bonus` added to `_owner_character_view` (save DC was already a computed field). |
| ~~**Passive Perception / Investigation / Insight**~~ | 10 + ability mod + (prof if proficient) | **Done.** Client-derived in the overlay (`dnd5e.js` `passiveScore`) from data already sent. |
| **Initiative** | Usually DEX mod | Not stored; the overlay shows `stat_modifiers.dex`. `_on_start_combat` already rolls real initiative — only the resting display was missing. |
| ~~**XP to next level**~~ | Bar / number toward the next threshold | **Done.** `_owner_character_view` sends `xp_level_start` / `xp_next_level` from `rules.xp_thresholds()`; the overlay shows `xp / next`. |
| ~~**Alignment**~~ | One field, no mechanics | **Done.** `CharacterSheet.alignment: str`, player-set via `character_edit` (in `CHARACTER_EDIT_TEXT_FIELDS`), fed to the DM through `character_summary`. |
| **Inspiration** | A single yes/no token | `CharacterSheet.inspiration: bool = False`; DM grants/spends it via an `update_character` field. |

## Small (one model field + a bit of engine logic)

| Field | Paper sheet | Add it |
|---|---|---|
| ~~**Temp HP**~~ | A separate pool absorbed before real HP | **Done.** `CharacterSheet.temp_hp`; `apply_update` drains it before real HP on damage, healing never touches it, a new source takes `max(current, new)` (no stacking). `update_character` gains a `temp_hp` field (Anthropic tool; the Ollama structured schema doesn't cover it — legacy tool-calling path can still send it). |
| ~~**Speed**~~ | Movement rate in feet | **Done (display-only).** `speed` per race in `srd.json` (30, dwarves 25, wood elf 35), `CharacterSheet.speed: int`, set in `build_starting_character`. No positioning system, so it's a number on the sheet and nothing more. |
| **Currency (CP/SP/EP/GP/PP)** | Five boxes; loot economy | `CharacterSheet.currency: dict[str,int]`; `update_character` gains `gold_delta` (or per-coin); DM system prompt gains "award/deduct coin when the fiction calls for it". Also wants a shop/price concept to be worth much. |

## Medium (model + engine + SRD data + DM prompt work)

| Field | Paper sheet | Add it |
|---|---|---|
| ~~**Personality Traits / Ideals / Bonds / Flaws**~~ | Four short RP fields | **Done.** Four `str` fields on `CharacterSheet`, seeded from `origins.json` at creation, player-editable via `character_edit`, fed to the DM through `character_summary`. No Inspiration mechanic yet (see below). |
| ~~**Attacks & Spellcasting table**~~ | Per-attack: name, to-hit bonus, damage + type | **Done.** `_owner_character_view` → `attacks: [{name, kind, to_hit, damage}]` — the equipped weapon (DEX for ranged, best of STR/DEX for finesse, else STR; magic bonus folded in; proficiency assumed) plus every attack-shaped known spell. Not covered: weapon-proficiency gating (Oracle tracks none — see below), cantrip damage scaling by level. |
| **Proficiencies & Languages** | Armor/weapon/tool proficiencies; spoken languages | `class.proficiencies` and `race.languages` into `srd.json`; surface in `_owner_character_view`. Display-only unless a proficiency actually gates something. |
| **Hit Dice pool** | `Nd<hit die>`, spent on a short rest, restored on a long rest | `CharacterSheet.hit_dice_total` (= level) / `hit_dice_remaining`. Replaces the current short-rest shortcut (`_apply_update`'s "heal half of what's missing") with the real "spend a die, roll it + CON" rule. Long rest restores half the pool. |

## Page 2 / cosmetic (probably just use Notes)

Appearance, age, height/weight, backstory, allies & organizations, character portrait,
treasure inventory beyond the item list. Oracle has the origin `background` blurb plus a
free-text `notes` field (editable via `character_edit`) — that likely covers this without
new model fields. Add structured fields only if a feature needs to read them.

## Explicitly not on the table

Multiclassing, subclasses, feats, encumbrance, exhaustion levels, attunement slots — none
are modeled anywhere in Oracle today and none are implied by anything here.
