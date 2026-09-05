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
| **Passive Perception** | 10 + WIS mod + (prof if Perception proficient) | Client-derived in the overlay from data already sent. Passive Investigation/Insight the same way if wanted. |
| **Initiative** | Usually DEX mod | Not stored; the overlay shows `stat_modifiers.dex`. `_on_start_combat` already rolls real initiative — only the resting display was missing. |
| **XP to next level** | Bar / number toward the next threshold | `xp` and `level` are sent; the SRD threshold table lives in `srd.json` (`leveling.xp_by_level`). Send `xp_to_next` from `_owner_character_view`, or ship the fixed table client-side. |
| **Alignment** | One field, no mechanics | `CharacterSheet.alignment: str = ""`, set at creation or via `character_edit`. Pure flavor / DM context. |
| **Inspiration** | A single yes/no token | `CharacterSheet.inspiration: bool = False`; DM grants/spends it via an `update_character` field. |

## Small (one model field + a bit of engine logic)

| Field | Paper sheet | Add it |
|---|---|---|
| **Temp HP** | A separate pool absorbed before real HP | `CharacterSheet.temp_hp: int = 0`; `apply_update` subtracts damage from `temp_hp` first, and a source sets it to `max(temp_hp, new)` (temp HP doesn't stack). `update_character` gains a `temp_hp` field. |
| **Speed** | Movement rate in feet | `race.speed` into `srd.json`, `CharacterSheet.speed: int`. Display-only until Oracle has any positioning/movement system — which it deliberately doesn't. Cheap to show, large to make mechanical. |
| **Currency (CP/SP/EP/GP/PP)** | Five boxes; loot economy | `CharacterSheet.currency: dict[str,int]`; `update_character` gains `gold_delta` (or per-coin); DM system prompt gains "award/deduct coin when the fiction calls for it". Also wants a shop/price concept to be worth much. |

## Medium (model + engine + SRD data + DM prompt work)

| Field | Paper sheet | Add it |
|---|---|---|
| ~~**Personality Traits / Ideals / Bonds / Flaws**~~ | Four short RP fields | **Done.** Four `str` fields on `CharacterSheet`, seeded from `origins.json` at creation, player-editable via `character_edit`, fed to the DM through `character_summary`. No Inspiration mechanic yet (see below). |
| **Attacks & Spellcasting table** | Per-attack: name, to-hit bonus, damage + type | `_owner_character_view` resolves `equipped_weapon` → `{name, atk_bonus: prof + STR/DEX mod, damage: die + mod + type}` from `srd.json` equipment data (all server-side already; just not shipped). Extend to known attack spells. |
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
