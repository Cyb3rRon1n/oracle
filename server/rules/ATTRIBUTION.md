# Data source and license

`srd.json` is a small, hand-curated starter set of game data (a few monsters,
spells, conditions, classes, equipment, and the character-advancement/
challenge-rating XP tables) drawn from the **D&D 5th Edition System
Reference Document 5.1**, released by Wizards of the Coast under the
**Creative Commons Attribution 4.0 International License**
(https://creativecommons.org/licenses/by/4.0/).

The `leveling` block (`xp_by_level`, `xp_by_cr`) is the SRD's own
Character Advancement and Experience Points by Challenge Rating tables in
full (levels 1-20, CR 0-30) - these are bare numeric tables with no
descriptive/flavor text, and including them whole (rather than the
hand-curated slice everything else in this file gets) is what lets
`server/engine.py`'s leveling system compute correct values for a level or
CR beyond whatever's currently in `monsters`/`classes`, without needing a
matching data addition every time those lists grow.

This is deliberately **not** a mirror of the full SRD, and it is **not**
content from the Player's Handbook, Dungeon Master's Guide, Monster Manual,
published adventures, or any other Wizards of the Coast sourcebook — that
material is copyrighted and not redistributable here. Only the freely
licensed SRD subset is included, and only a small slice of it, to keep the
retrieval architecture demonstrably correct without taking on a large,
unverified data-ingestion job.

Everything else the Dungeon Master narrates — settings, NPCs, quests,
homebrew monsters — is generated fresh by the LLM and is not sourced from
this file.

Expanding coverage (more monsters, spells, full class progressions) is
future work; any addition should stay within SRD-licensed content.
