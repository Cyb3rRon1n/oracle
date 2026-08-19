from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

# The six SRD ability scores, in the SRD's own conventional order - shared
# by both player CharacterSheets (stats, populated by
# server/engine.py's build_starting_character) and NPC stat blocks
# (server/rules/srd.json's monsters already use this exact key set,
# e.g. goblin's "stats": {"str": 8, "dex": 14, ...} - so a player's own
# `stats` dict now speaks the same shape the DM already sees for every
# monster via lookup_rule, not a second, disconnected convention).
ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

# Real 5e's own 18 skills and the ability each one is governed by (Basic
# Rules Chapter 7) - fixed, real-world data, not per-class content, so
# this lives here as a plain dict rather than in server/rules/srd.json
# alongside monster/equipment/class content. Lives in this module (not
# server/engine.py, where the per-class proficiency logic that actually
# uses it lives) so server/narrator.py's request_roll tool schema can
# import it too without a circular import - narrator.py already imports
# from this module, and engine.py already imports from narrator.py.
SKILL_ABILITIES: dict[str, str] = {
    "athletics": "str",
    "acrobatics": "dex", "sleight_of_hand": "dex", "stealth": "dex",
    "arcana": "int", "history": "int", "investigation": "int", "nature": "int", "religion": "int",
    "animal_handling": "wis", "insight": "wis", "medicine": "wis", "perception": "wis", "survival": "wis",
    "deception": "cha", "intimidation": "cha", "performance": "cha", "persuasion": "cha",
}

# Which ability each of Oracle's two real spellcasting classes uses (real
# 5e Basic Rules - Wizard: Intelligence, Cleric: Wisdom). Lives here
# rather than server/engine.py for the same circular-import reason
# SKILL_ABILITIES does - the spell_save_dc computed field just below
# needs it, and a CharacterSheet computed field can't reach into
# engine.py (which imports from this module, not the other way around).
# Fighter/rogue have no entry and cast nothing, the same "no entry means
# not applicable" convention CLASS_ABILITY_PRIORITY's own absence for an
# unrecognized class already establishes.
SPELLCASTING_ABILITY: dict[str, str] = {"wizard": "int", "cleric": "wis"}


def ability_modifier(score: int) -> int:
    """The standard 5e ability-modifier formula - floor((score-10)/2).
    A module-level function, not a method, so server/engine.py can apply it
    to a bare score (e.g. computing HP growth from a class's hit die plus a
    CON score) without needing a CharacterSheet instance in hand."""
    return (score - 10) // 2


def proficiency_bonus_for_level(level: int) -> int:
    """The standard 5e proficiency-bonus-by-level formula - +2 at levels
    1-4, rising by 1 every 4 levels thereafter (5-8: +3, ..., 17-20: +6).
    A module-level function, not a method, for the same reason
    ability_modifier is - server/engine.py's request_roll closure applies
    this to the acting character's real level for a skill check, without
    needing a full CharacterSheet in hand for the bare formula itself."""
    return 2 + (level - 1) // 4


class InventoryItem(BaseModel):
    """A single carried stack - server/rules/srd.json's own equipment
    name, a count (real 5e stacks identical items - two potions of
    healing are one stack of 2, not two separate list entries the way a
    plain string list used to force), and an optional flat magic_bonus:
    a DM-granted enhancement (e.g. update_character's own add_item +
    magic_bonus="1" narrating a found +1 longsword) applied on top of
    the item's own real SRD base stats (_compute_ac/request_roll's
    weapon-damage resolution, server/engine.py) rather than replacing
    them - the "structured item objects with real special properties"
    gap named and deferred twice already (ROADMAP.md items 9 and 13).

    Two stacks can share the same name but a different magic_bonus (a
    mundane Longsword and a +1 Longsword aren't the same stack) - a
    real, deliberate limitation for now: equip/unequip/remove_item all
    still resolve a bare name to "whichever stack matches first"
    (find_item, below), the same name-based convention this project's
    equipped_weapon/equipped_armor/equipped_shield pointers already
    use, not a full per-item id system."""

    name: str
    quantity: int = 1
    magic_bonus: int = 0


class CharacterSheet(BaseModel):
    player_id: str
    name: str
    hp: int
    max_hp: int
    character_class: str = ""
    # A real, deliberately separate concept from character_class - server/
    # rules/srd.json's own "races" table (server/engine.py's
    # build_starting_character reads it the same way it already reads
    # "classes"). Blank means no recognized race was chosen, the same
    # graceful-miss convention character_class's own blank default already
    # establishes - no ability bonus, no racial traits, not an error.
    # Explicitly deferred when the tabbed character sheet shipped
    # (ROADMAP.md item 7) until a real race system existed to back it.
    race: str = ""
    stats: dict[str, int] = Field(default_factory=dict)
    # A list of InventoryItem stacks, not plain name strings - closes the
    # "structured item objects with real special properties" gap named
    # and explicitly deferred twice already (ROADMAP.md items 9 and 13):
    # real quantities (a stack, not N duplicate entries) and a real
    # magic_bonus, both previously impossible to represent.
    inventory: list[InventoryItem] = Field(default_factory=list)
    # Pointers into inventory by name (find_item, above), not a separate
    # item store or an id - still just strings, since the equip/unequip
    # mechanic only ever needs "which name is currently worn/wielded",
    # not a reference to one specific stack among several sharing that
    # name (InventoryItem's own docstring covers that edge case). None
    # means nothing in that slot (unarmored, or fighting bare-handed).
    equipped_weapon: str | None = None
    equipped_armor: str | None = None
    # A real second equipment slot, not another armor pointer - a shield's
    # +2 AC (server/rules/srd.json's own "shield" entry) is additive on
    # top of whatever's in equipped_armor, not a replacement base value
    # the way equipped_armor's own `ac` field is - see _compute_ac
    # (server/engine.py) for the real formula this feeds. Closes a real,
    # previously-documented gap: "Structured Equipment" (ROADMAP.md)
    # originally shipped a single equipped_armor slot specifically
    # because a shield couldn't be represented in it without silently
    # computing AC wrong.
    equipped_shield: str | None = None
    conditions: list[str] = Field(default_factory=list)
    notes: str = ""
    # Who this character was before Aetherfall - generated once at
    # creation (server/engine.py's build_starting_character, via
    # server/lore's random_origin) and never regenerated, a fixed fact
    # about the character rather than something that should drift.
    # Blank for an NPC (server/engine.py's introduce-on-first-mention
    # path never sets this) and for any character sheet predating this
    # field - real, old sessions/*.json data, not required by
    # model_validate_json's own default.
    background: str = ""
    xp: int = 0
    level: int = 1
    # Real 5e's own unarmored baseline (10 + DEX modifier), or an equipped
    # armor's own base AC + DEX modifier - computed by server/engine.py's
    # _compute_ac (both at character creation and again on every
    # equip/unequip via _on_character_edit), which has both the SRD
    # equipment data and the character's own stats in hand; a plain stored
    # field, not a computed one like stat_modifiers, since a real 5e
    # monster's AC (copied verbatim from srd.json onto a tracked NPC) is a
    # flat authored value, not a formula derived from its own stats/gear -
    # the two need genuinely different sources, so one field can't be
    # computed from the sheet alone for both roles. Now genuinely live
    # (recomputed on every equip/unequip), closing the gap this comment
    # used to flag - see docs/protocol.md's "Structured equipment" section.
    ac: int = 10
    # Real 5e's death-saving-throw mechanic, closing the gap that's existed
    # since HP was first tracked: hitting 0 HP was previously just a number
    # sitting there, cosmetically red on the client's HP bar, with no real
    # stakes attached. Deliberately scoped to player characters only, never
    # NPCs - a defeated NPC already has its own, different, already-shipped
    # 0-HP behavior (server/engine.py's apply_update closure treats an
    # NPC's own hp>0-to-0 crossing as "defeated", awarding XP immediately -
    # real 5e's own actual rule for ordinary monsters, which die outright
    # at 0 HP rather than making death saves; only player characters -  and,
    # in real 5e, important NPCs the DM chooses to grant the same treatment
    # to, not modeled here - get this). dying is true from the moment HP
    # first reaches 0 until either 3 successes stabilize it or 3 failures
    # end it - a stable-but-unconscious character (3 successes reached) has
    # dying=False and hp==0 simultaneously, distinct from a fresh full-
    # health sheet the same way dead below is.
    dying: bool = False
    dead: bool = False
    death_save_successes: int = 0
    death_save_failures: int = 0
    # A structured, cheaper alternative to the who-knows-whom relationship
    # graph flagged as real future work (see ROADMAP.md) - not who an NPC
    # knows or a full personality, just a coarse attitude the DM can stay
    # consistent against turn to turn instead of only inferring it from
    # free-text notes. Meaningful for a tracked NPC (Session.npcs); on a
    # real player character it just sits at its default, unused - the same
    # shared-model tradeoff notes/ac already make (see their own comments)
    # rather than a second, NPC-only sheet class for one field.
    disposition: Literal["hostile", "neutral", "friendly"] = "neutral"
    # Spellcasting - real 5e's own two mechanical resources, both plain
    # stored fields (not computed, unlike stat_modifiers/proficiency_bonus)
    # since both genuinely mutate over a session: known_spells is set once
    # at creation (server/engine.py's build_starting_character, the same
    # "no player-chosen allocation yet" scope every other stat-generation
    # step here already has) but spell_slots is spent and restored
    # constantly (casting, resting, leveling up). max_spell_slots is a
    # separate stored field, not derived live from level, because
    # spell_slots itself needs a persistent "how many are currently spent"
    # number that survives a save/reload independent of level - the same
    # reason max_hp is a real field, not computed from hit_die+level either.
    # Empty for a non-caster (fighter/rogue, or a blank/unrecognized
    # class) - the same fallback stats/inventory already use.
    known_spells: list[str] = Field(default_factory=list)
    spell_slots: dict[str, int] = Field(default_factory=dict)
    max_spell_slots: dict[str, int] = Field(default_factory=dict)

    @computed_field
    @property
    def spell_save_dc(self) -> int | None:
        """Real 5e's own formula: 8 + proficiency bonus + spellcasting
        ability modifier - precomputed the same "don't rely on the LLM for
        arithmetic" reasoning stat_modifiers/proficiency_bonus already
        follow. None for a non-caster (SPELLCASTING_ABILITY has no entry
        for its class, or stats is empty) - a real "not applicable" signal,
        not a fabricated number for a fighter/rogue/blank-class sheet."""
        ability = SPELLCASTING_ABILITY.get(self.character_class.strip().lower())
        if ability is None or ability not in self.stat_modifiers:
            return None
        return 8 + self.proficiency_bonus + self.stat_modifiers[ability]

    @computed_field
    @property
    def stat_modifiers(self) -> dict[str, int]:
        """Precomputed ability modifiers, included in model_dump()/
        model_dump_json() output automatically (a pydantic v2
        @computed_field) - so the DM's character_summary, and the
        engine's own request_roll closure, both read a modifier directly
        rather than recomputing floor((score-10)/2) themselves. This
        project's whole XP-award design already rejected relying on an
        LLM to get arithmetic right when the engine can just do it
        (server/engine.py's DEFAULT_NPC_XP/apply_update comments); this is
        the same principle applied to ability scores. Empty when `stats`
        is empty (a blank/unrecognized class, or any NPC/legacy sheet with
        no stats populated) - not an error, the same "not present isn't
        an error" convention the rest of this codebase already follows."""
        return {key: ability_modifier(score) for key, score in self.stats.items()}

    @computed_field
    @property
    def proficiency_bonus(self) -> int:
        """Precomputed from the real level-based formula (see
        proficiency_bonus_for_level above), included in model_dump()/
        model_dump_json() automatically - the same "don't rely on the LLM
        to get arithmetic right when the engine can just do it" reasoning
        stat_modifiers already follows, applied to skill proficiencies.
        Present on every character regardless of class or whether it has
        any proficient skills at all - it's purely a function of level,
        real 5e's own actual rule (proficiency bonus applies to saving
        throws and other proficient rolls too, not just skills)."""
        return proficiency_bonus_for_level(self.level)

    def gain_xp(self, amount: int, xp_thresholds: dict[int, int]) -> int:
        """Awards XP and applies any level-ups the new total crosses -
        looped, not a single if, since one award (a tough kill, or several
        stacked awards in one turn) can plausibly cross more than one
        threshold at once. Returns how many levels were gained (0 if none)
        so a caller can decide whether to announce a level-up.

        xp_thresholds is a level -> cumulative-XP-required-to-reach-it
        table (server/rules/srd.json's "leveling.xp_by_level", the SRD's
        own real Character Advancement table) passed in rather than looked
        up here - this module has no access to rules data, and reaching for
        it directly would make CharacterSheet depend on server.rules for a
        single method, which server/engine.py (the only real caller,
        already holding a RulesIndex) is better placed to own.

        Deliberately no HP growth here - that needs the character's class
        hit die, which lives in rules data alongside the XP tables, not on
        the sheet itself. server/engine.py applies HP growth right after
        calling this, the same "state.py owns mechanical bookkeeping,
        engine.py owns anything needing rules data" split
        build_starting_character already follows."""
        if amount <= 0:
            return 0
        self.xp += amount
        levels_gained = 0
        max_level = max(xp_thresholds, default=self.level)
        while self.level < max_level and self.xp >= xp_thresholds.get(self.level + 1, float("inf")):
            self.level += 1
            levels_gained += 1
        return levels_gained

    def find_item(self, name: str | None) -> InventoryItem | None:
        """The first inventory stack whose name matches (case-insensitive) -
        "whichever comes first" is the same resolution equip/unequip/
        remove_item all use for a name that could match more than one
        stack (see InventoryItem's own docstring on why that can happen).
        None for a blank name or no match, the same graceful-miss
        convention every other name-based lookup in this project follows."""
        if not name:
            return None
        normalized = name.strip().lower()
        return next((item for item in self.inventory if item.name.strip().lower() == normalized), None)

    def add_item(self, name: str, magic_bonus: int = 0) -> InventoryItem:
        """Adds one of `name` to inventory - stacks onto an existing entry
        with the same name AND the same magic_bonus (real 5e's own
        "identical items stack" convention), rather than always appending
        a new entry the way a plain string list used to force. A
        genuinely different item (the same base name but a different
        enchantment) gets its own separate stack instead of merging into
        one that would misrepresent it."""
        for item in self.inventory:
            if item.name.strip().lower() == name.strip().lower() and item.magic_bonus == magic_bonus:
                item.quantity += 1
                return item
        item = InventoryItem(name=name, magic_bonus=magic_bonus)
        self.inventory.append(item)
        return item

    def remove_item(self, name: str) -> bool:
        """Removes one of `name` from whichever stack matches first
        (find_item, above) - decrements its quantity, dropping the stack
        entirely once it reaches zero rather than leaving a zero-quantity
        entry behind. Returns whether a matching stack actually existed,
        so callers (apply_update below, server/engine.py's character_edit
        handling) can tell a real removal from a no-op the same way the
        old `remove_item in self.inventory` check already did."""
        item = self.find_item(name)
        if item is None:
            return False
        item.quantity -= 1
        if item.quantity <= 0:
            self.inventory.remove(item)
        return True

    def apply_update(self, update: dict) -> str:
        """Apply a DM-issued mechanical update (the update_character tool).
        Returns a human-readable summary of what changed, for the tool_result
        the DM sees back."""
        changes: list[str] = []

        hp_delta = update.get("hp_delta")
        if hp_delta:
            prior_hp = self.hp
            prior_dying = self.dying
            self.hp = max(0, min(self.max_hp, self.hp + int(hp_delta)))
            sign = "+" if hp_delta > 0 else ""
            changes.append(f"HP {sign}{hp_delta} (now {self.hp}/{self.max_hp})")

            if hp_delta < 0 and prior_hp == 0 and prior_dying and not self.dead:
                # Already down and dying - taking more damage while at 0 HP
                # is an automatic death-save failure under real 5e's own
                # rule, not something that waits for the next /deathsave.
                # A real, deliberate simplification: real 5e doubles this to
                # two failures on a critical hit, but nothing in this
                # project tracks whether a hit was a critical (server/dice.py
                # has no crit concept at all), so every hit while down counts
                # as a single failure here.
                changes.append(self.record_death_save(success=False))
            elif hp_delta < 0 and prior_hp > 0 and self.hp == 0 and not self.dead:
                self.dying = True
                self.death_save_successes = 0
                self.death_save_failures = 0
                changes.append(f"{self.name} drops to 0 HP and begins dying - roll a death save")

        # A real recovery mechanic, closing a gap that's existed since HP
        # was first tracked: healing had always meant the DM narrating a
        # positive hp_delta and doing that arithmetic itself - the same
        # "don't rely on the model to get numbers right when the engine
        # can just compute them" reasoning ability scores/XP already
        # follow, applied here. Deliberately simplified from real 5e (no
        # hit-dice pool, no per-die CON-modifier healing) - a long rest is
        # a full, unconditional HP restore (real 5e's own actual rule, not
        # a simplification); a short rest restores half of whatever's
        # currently missing, a proportional stand-in for "spend some hit
        # dice" that needs no new resource tracked on the sheet.
        # Deliberately doesn't touch conditions - unlike HP, most SRD
        # conditions (poisoned, frightened, ...) don't just expire with
        # time under the actual rules, so silently clearing them here
        # would be a real rules error, not a simplification; the DM can
        # still pair this with an explicit remove_condition in the same
        # call when the fiction actually calls for it.
        rest = update.get("rest")
        if rest == "long" and self.hp < self.max_hp:
            self.hp = self.max_hp
            changes.append(f"long rest: HP restored to {self.hp}/{self.max_hp}")
        elif rest == "short":
            healed = (self.max_hp - self.hp) // 2
            if healed > 0:
                self.hp += healed
                changes.append(f"short rest: HP +{healed} (now {self.hp}/{self.max_hp})")

        # Healing above 0 HP - whether from hp_delta or either rest branch
        # above, checked once here rather than duplicated in both - clears
        # dying and resets the death-save count the same way waking up from
        # unconsciousness would. Never resurrects a dead character (dead is
        # permanent in this project's scope - no resurrection mechanic
        # exists at all, since there's no spellcasting yet either).
        if self.hp > 0 and self.dying and not self.dead:
            self.dying = False
            self.death_save_successes = 0
            self.death_save_failures = 0
            changes.append(f"{self.name} is healed above 0 HP and stabilizes")

        add_item = update.get("add_item")
        if add_item:
            # magic_bonus only ever comes from the DM's own tool call, never
            # from a player's own character_edit add_item (server/engine.py) -
            # the same "the engine or the DM decides mechanical state, the
            # player only decides fiction/bookkeeping" boundary every other
            # mechanical field already draws.
            magic_bonus = update.get("magic_bonus") or 0
            self.add_item(add_item, magic_bonus=magic_bonus)
            label = f"+{magic_bonus} {add_item}" if magic_bonus else add_item
            changes.append(f"gained '{label}'")

        remove_item = update.get("remove_item")
        if remove_item and self.remove_item(remove_item):
            changes.append(f"lost '{remove_item}'")

        add_condition = update.get("add_condition")
        if add_condition and add_condition not in self.conditions:
            self.conditions.append(add_condition)
            changes.append(f"now {add_condition}")

        remove_condition = update.get("remove_condition")
        if remove_condition and remove_condition in self.conditions:
            self.conditions.remove(remove_condition)
            changes.append(f"no longer {remove_condition}")

        notes = update.get("notes")
        if notes and notes != self.notes:
            self.notes = notes
            changes.append("notes updated")

        disposition = update.get("disposition")
        # A real model-input boundary, not decorative: disposition is a
        # closed enum on the model (Literal["hostile", "neutral",
        # "friendly"]), but this dict comes straight from a tool call - the
        # JSON schema's own "enum" constrains AnthropicNarrator, but nothing
        # stops OllamaNarrator's shared update_character path from sending
        # an arbitrary string. A plain attribute assignment here wouldn't
        # re-validate against the Literal (pydantic v2 doesn't, by default,
        # on direct attribute sets), so an unrecognized value is silently
        # ignored rather than corrupting the field's own declared contract.
        if disposition in ("hostile", "neutral", "friendly") and disposition != self.disposition:
            self.disposition = disposition
            changes.append(f"disposition now {disposition}")

        if not changes:
            return "No changes applied (nothing matched, or all deltas were zero)."
        return "Applied: " + "; ".join(changes) + "."

    def record_death_save(self, *, success: bool, count: int = 1) -> str:
        """Records one or more death-save outcomes and resolves stabilize/
        death if a threshold is crossed - the one place that logic lives,
        shared by apply_update's own "took damage while already down"
        automatic-failure trigger above and GameEngine._on_death_save's
        explicit /deathsave roll (server/engine.py), so the real 5e
        3-successes/3-failures threshold can't drift between the two
        triggers. No leading underscore, unlike this module's other private
        helpers - engine.py is a real, intended caller of this one, the
        same "state.py owns bookkeeping, engine.py reaches in for it"
        relationship apply_update/gain_xp already have. count=2 is a
        natural 1 on the d20 roll itself (real 5e: counts as two failures)
        - looped rather than added directly so a nat 1 that would already
        be the third failure stops there instead of recording a fourth
        that can never matter."""
        for _ in range(count):
            if success:
                self.death_save_successes += 1
            else:
                self.death_save_failures += 1

            if self.death_save_successes >= 3:
                self.dying = False
                self.death_save_successes = 0
                self.death_save_failures = 0
                return f"{self.name} stabilizes"
            if self.death_save_failures >= 3:
                self.dying = False
                self.dead = True
                return f"{self.name} has died"

        return (
            f"death save {'success' if success else 'failure'} "
            f"({self.death_save_successes} successes, {self.death_save_failures} failures)"
        )


class Objective(BaseModel):
    text: str
    # expired/failed close a real, previously-named gap: only active/
    # completed existed, so nothing could represent a quest going stale or
    # being failed outright - every objective either stayed open forever or
    # eventually got a success. Two distinct terminal states, not one
    # generic "closed," since "the caravan already left without you" reads
    # differently from "you drove off the bandits" and a DM/player
    # shouldn't have to infer which happened from a missing objective alone.
    status: Literal["active", "completed", "expired", "failed"] = "active"


class WorldState(BaseModel):
    location: str = "unknown"
    summary: str = ""
    mood: str = ""
    flags: dict[str, bool] = Field(default_factory=dict)
    objectives: list[Objective] = Field(default_factory=list)
    # A real graph, not a 2D grid - ROADMAP.md item 8 scoped this as the
    # near-term, buildable-now half of "is there room for a map/visual
    # panel" (the other half, real image rendering, needs a terminal
    # graphics protocol and a generation pipeline neither of which exist
    # yet). Keyed by location name -> the names of every location directly
    # connected to it; edges are always added symmetrically (see
    # apply_update's connect_locations handling below), so a real dungeon's
    # two-way passages don't need the DM to declare both directions. No
    # coordinates/layout - an adjacency list renders correctly regardless
    # of the graph's real shape, unlike a 2D grid which would need the DM
    # to supply consistent x/y positions.
    location_map: dict[str, list[str]] = Field(default_factory=dict)

    def apply_update(self, update: dict) -> str:
        """Apply a DM-issued world-state update (the update_world tool).
        Returns a human-readable summary of what changed, for the tool_result
        the DM sees back. Mirrors CharacterSheet.apply_update()'s pattern."""
        changes: list[str] = []

        location = update.get("location")
        if location and location != self.location:
            self.location = location
            changes.append(f"location now '{location}'")

        summary = update.get("summary")
        if summary and summary != self.summary:
            self.summary = summary
            changes.append("summary updated")

        mood = update.get("mood")
        if mood and mood != self.mood:
            self.mood = mood
            changes.append(f"mood now '{mood}'")

        add_objective = update.get("add_objective")
        if add_objective and not any(o.text == add_objective for o in self.objectives):
            self.objectives.append(Objective(text=add_objective))
            changes.append(f"new objective: '{add_objective}'")

        # complete/expire/fail all only ever fire from "active" - with a
        # single terminal state ("completed") the old guard here
        # (status != "completed") and "status == active" were equivalent,
        # but adding expired/failed made them genuinely different: without
        # this, a failed objective could still be flipped to completed by
        # a later complete_objective call, since "failed" != "completed"
        # too. A real terminal state shouldn't be overwritten by a
        # different terminal state, whichever of the three it already is.
        complete_objective = update.get("complete_objective")
        if complete_objective:
            for objective in self.objectives:
                if objective.text == complete_objective and objective.status == "active":
                    objective.status = "completed"
                    changes.append(f"completed: '{complete_objective}'")
                    break

        expire_objective = update.get("expire_objective")
        if expire_objective:
            for objective in self.objectives:
                if objective.text == expire_objective and objective.status == "active":
                    objective.status = "expired"
                    changes.append(f"expired: '{expire_objective}'")
                    break

        fail_objective = update.get("fail_objective")
        if fail_objective:
            for objective in self.objectives:
                if objective.text == fail_objective and objective.status == "active":
                    objective.status = "failed"
                    changes.append(f"failed: '{fail_objective}'")
                    break

        remove_objective = update.get("remove_objective")
        if remove_objective:
            before = len(self.objectives)
            self.objectives = [o for o in self.objectives if o.text != remove_objective]
            if len(self.objectives) < before:
                changes.append(f"removed objective: '{remove_objective}'")

        set_flag = update.get("set_flag")
        if set_flag and not self.flags.get(set_flag):
            self.flags[set_flag] = True
            changes.append(f"flag set: {set_flag}")

        clear_flag = update.get("clear_flag")
        if clear_flag and self.flags.get(clear_flag):
            self.flags[clear_flag] = False
            changes.append(f"flag cleared: {clear_flag}")

        add_location = update.get("add_location")
        if add_location and add_location not in self.location_map:
            self.location_map[add_location] = []
            changes.append(f"added location: '{add_location}'")

        # A two-way passage, not a one-way exit - real dungeon layouts are
        # overwhelmingly two-way, and modeling a one-way exit as a distinct
        # case would need a second tool field for a genuinely rare shape.
        # a != b guards a degenerate self-connection from appending the
        # same name to the same list twice (self.location_map[a] and
        # self.location_map[b] would be the same list object).
        connect_locations = update.get("connect_locations")
        if connect_locations and len(connect_locations) == 2 and connect_locations[0] != connect_locations[1]:
            a, b = connect_locations
            self.location_map.setdefault(a, [])
            self.location_map.setdefault(b, [])
            if b not in self.location_map[a]:
                self.location_map[a].append(b)
                self.location_map[b].append(a)
                changes.append(f"connected '{a}' and '{b}'")

        if not changes:
            return "No changes applied (nothing matched, or all deltas were zero)."
        return "Applied: " + "; ".join(changes) + "."

    def narrator_context(self) -> str:
        """Plain-text current location + active objectives, given to the DM
        on every turn as NarratorBackend.narrate()'s own world_summary
        argument - not left for the model to infer or recall from `history`
        alone. Built to test a specific hypothesis for complete_objective's
        own 0% measured reliability (ROADMAP.md's update_world
        investigation): that it was a *recall* problem - a small model
        needing to retype an objective's exact text correctly from several
        turns back. That hypothesis measured as wrong (re-tested across 10
        repeat runs with the exact text sitting directly in the prompt via
        this same method: still 0/10) - see server/narrator_ollama.py's
        WORLD_UPDATE_PROMPT_ADDENDUM comment for the full writeup. Kept
        anyway as real, defensible infrastructure - grounding
        location/add_objective in the session's actual current state
        rather than nothing measured as not worse than not having it - but
        this alone does not fix complete_objective. "" when there's
        nothing to report yet (a fresh session, no location or active
        objectives set) - the same "don't render the absent default"
        convention this project's other optional summaries (_resume_recap,
        server/engine.py) already follow."""
        parts = []
        if self.location and self.location != "unknown":
            parts.append(f"Current location: {self.location}")
        if self.mood:
            parts.append(f"Current mood: {self.mood}")
        active_objectives = [o.text for o in self.objectives if o.status == "active"]
        if active_objectives:
            parts.append("Active objectives:\n" + "\n".join(f"- {text}" for text in active_objectives))
        return "\n".join(parts)


MAX_HISTORY_MESSAGES = 12  # 6 player-action/DM-narration exchanges


class Session(BaseModel):
    session_id: str
    characters: dict[str, CharacterSheet] = Field(default_factory=dict)
    npcs: dict[str, CharacterSheet] = Field(default_factory=dict)
    world: WorldState = Field(default_factory=WorldState)
    turn_order: list[str] = Field(default_factory=list)
    current_turn_index: int = 0
    # Whether the pre-game lobby has been left via an explicit start_session
    # (server/engine.py's GameEngine._on_start_session) - False is the
    # correct default for a genuinely fresh session, but also for any
    # session saved before this field existed. GameEngine.handle() never
    # trusts this field alone for that reason: it treats `started or
    # bool(log)` as "has the adventure begun", so an old real save with
    # actual narration history (this project's own real sessions/*.json,
    # among others) is correctly still recognized as already-started even
    # though it predates this field and would otherwise load as False.
    started: bool = False
    # Real 5e formal initiative (server/engine.py's _on_start_combat/
    # _on_end_combat) - deliberately narrow scope: only replaces the
    # mechanical turn_order/current_turn-index cycling for the duration of
    # a fight, never a second turn-tracking system running in parallel.
    # pre_combat_turn_order is a snapshot of turn_order taken the moment
    # combat starts (which, since combat hasn't started yet, is still
    # plain join order at that point) - restored (plus anyone who joined
    # mid-combat, appended to the end) when combat ends, rather than a
    # second persistent "join order" field that would need its own
    # backward-compatibility handling for sessions saved before this
    # existed. None outside combat.
    in_combat: bool = False
    pre_combat_turn_order: list[str] | None = None
    log: list[dict] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
    # player_ids whose *next* turn should get a DM-facing recap prepended
    # to their action text (server/engine.py's _on_player_action) -
    # addresses a real, distinct gap from _resume_recap()'s own player-
    # facing "story so far" message: the rolling history window
    # (max_history_messages) only ever holds the last few turns, so a
    # player reconnecting after a long gap and then acting again could
    # have the DM narrate against a context that's already scrolled past
    # anything relevant to them - the player gets a recap, but the model
    # generating the *next* narration doesn't. Persisted (not a plain
    # engine-instance attribute) since a reconnect can happen across a
    # server restart, the same reason turn_order/current_turn_index are
    # persisted rather than kept purely in memory. Set in
    # _on_join_session's reconnect branch, consumed (popped) the moment
    # that player's next player_action actually arrives - a real
    # multi-session gap between join and first action doesn't re-trigger
    # this every join, only the one time.
    pending_dm_recap: list[str] = Field(default_factory=list)
    # A lightweight session-zero choice (server/engine.py's
    # _on_start_session), set once by whoever starts the adventure - a
    # real tabletop practice (agreeing on tone/intensity before play
    # begins), not previously offered at all. "standard" needs no special
    # handling (WorldBible's own tone_guidance already covers it); a
    # non-default choice adds a real per-turn instruction to the DM (see
    # CONTENT_PREFERENCE_HINTS) rather than only being stated once and
    # risking it scrolling out of the rolling history window, the same
    # "durable fact, not a one-time mention" reasoning WorldBible's own
    # system-prompt placement already established - session-scoped rather
    # than baked into the narrator's shared system prompt, since one
    # server process can host multiple sessions with different choices.
    content_preference: Literal["lighter", "standard", "intense"] = "standard"
    # Per-instance so it can be tuned for a real production session, or
    # varied experimentally (see scripts/live_reliability_check.py's
    # --max-history-messages) without changing the shipped default here.
    max_history_messages: int = MAX_HISTORY_MESSAGES

    @property
    def current_turn(self) -> str | None:
        if not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index % len(self.turn_order)]

    def advance_turn(self) -> None:
        if self.turn_order:
            self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)

    def append_turn(self, action_text: str, narration_text: str) -> None:
        """Record a resolved turn in the rolling conversation window fed to the DM."""
        self.history.append({"role": "user", "content": action_text})
        self.history.append({"role": "assistant", "content": narration_text})
        if len(self.history) > self.max_history_messages:
            self.history = self.history[-self.max_history_messages :] if self.max_history_messages > 0 else []
