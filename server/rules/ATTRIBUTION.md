# Data source and license

`srd.json` is a small, hand-curated starter set of game data (a few monsters,
spells, conditions, classes, races, equipment, and the character-advancement/
challenge-rating XP tables) drawn from the **D&D 5th Edition System
Reference Document 5.1**, released by Wizards of the Coast under the
**Creative Commons Attribution 4.0 International License**
(https://creativecommons.org/licenses/by/4.0/).

The `leveling` block (`xp_by_level`, `xp_by_cr`, `spell_slots_by_level`) is
the SRD's own Character Advancement, Experience Points by Challenge Rating,
and full-caster Spell Slots by Level tables in full (levels 1-20, CR 0-30)
- these are bare numeric tables with no descriptive/flavor text, and
including them whole (rather than the hand-curated slice everything else in
this file gets) is what lets `server/engine.py`'s leveling/spellcasting
system compute correct values for a level or CR beyond whatever's currently
in `monsters`/`classes`/`spells`, without needing a matching data addition
every time those lists grow.

This is deliberately **not** a mirror of the full SRD, and it is **not**
content from the Player's Handbook, Dungeon Master's Guide, Monster Manual,
published adventures, or any other Wizards of the Coast sourcebook — that
material is copyrighted and not redistributable here. Only the freely
licensed SRD subset is included, to keep the retrieval architecture
demonstrably correct without taking on a large, unverified data-ingestion
job.

Everything else the Dungeon Master narrates — settings, NPCs, quests,
homebrew monsters — is generated fresh by the LLM and is not sourced from
this file.

## Equipment coverage (2026-08-10)

`equipment` is the one category that aims for real SRD *completeness*
rather than a small hand-picked slice — the full SRD 5.1 weapon table
(simple/martial, melee/ranged), the full armor table (light/medium/heavy,
plus a real, wearable `shield` entry — see below), a broad adventuring
gear list, the standard equipment packs, and the SRD's own small generic
magic item list (`+1 Weapon`, `Bag of Holding`, etc. — not the Dungeon
Master's Guide's much larger magic item catalog, which isn't SRD-licensed
at all).

One deliberate scoping choice, not an oversight:
- **Tools are a representative subset, not all ~30 SRD variants.** Real
  5e lists 17 near-identical Artisan's Tools types (Smith's Tools,
  Weaver's Tools, ...) differing only by name/cost, plus a similarly long
  musical instrument list. A handful of each (and every tool with real
  distinct mechanical text — Thieves' Tools, Herbalism Kit, Disguise Kit,
  Forgery Kit, Navigator's Tools, Poisoner's Kit) are included in full;
  the rest weren't, since they'd add JSON volume without adding anything
  a DM or player could do differently.

`shield` has a real `ac_bonus` field (2026-08-10), not an `ac` field -
a shield's +2 AC is additive on top of whatever body armor is also worn,
not a replacement base value the way `ac` is for real armor. `equip`/
`unequip`/`_compute_ac` (`server/engine.py`) now have a genuine third
slot (`CharacterSheet.equipped_shield`, `server/state.py`) specifically
so this stacks correctly instead of silently computing AC wrong - closes
what this section originally documented as a real, not-yet-built gap.

Medium and heavy armor's AC formulas (`_compute_ac`) got real support at
the same time this data landed — a Dex bonus capped at a real maximum for
medium armor, none at all for heavy — since shipping the data without it
would have been a real, known correctness bug (the exact trap this file's
own "Deliberately did not add heavier armor" note, prior to this
expansion, existed specifically to avoid).

## Race coverage (2026-08-11, subraces added the same day)

`races` covers the SRD's four core playable races - Human, Elf, Dwarf,
Halfling - each with its own real `ability_score_increase` and `traits`
text, plus the real SRD subrace for each race that has one: High Elf and
Wood Elf, Hill Dwarf and Mountain Dwarf, Lightfoot Halfling and Stout
Halfling (Human has no subrace in the SRD - a real asymmetry, not a gap).
Each subrace entry is self-contained - its `ability_score_increase`/
`traits` already include the base race's own bonus and traits combined
with the subrace's, rather than requiring `server/engine.py` to stack two
separate lookups at character-creation time.

Expanding coverage further (more monsters, spells, full class
progressions) is future work; any addition should stay within
SRD-licensed content.
