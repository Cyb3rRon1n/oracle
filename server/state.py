from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from . import dice

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


def _clamp_int(value: object, lo: int, hi: int) -> int | None:
    """Best-effort coercion + clamping of a DM-tool-supplied number to
    [lo, hi]. The model may type '3' or 3.0 where an int belongs; anything
    unparseable degrades to None instead of raising, the same graceful-miss
    convention every other name-based lookup in this project follows."""
    try:
        return min(hi, max(lo, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
    # Buffer drained before real HP on damage, untouched by healing, no stacking.
    temp_hp: int = 0
    character_class: str = ""
    # The class hit die, e.g. "d10", set at creation. Blank for a classless
    # character - short rests fall back to the flat "heal half" stand-in.
    hit_die: str = ""
    # 5e hit-dice pool: total == level, spent one at a time on a short rest to
    # heal (roll + CON), half restored on a long rest.
    hit_dice_total: int = 1
    hit_dice_remaining: int = 1
    # Independent of class; blank = no recognized race (no bonus/traits, not an error).
    race: str = ""
    # Walking speed in feet, from the race (default 30). Display-only - Oracle
    # has no positioning system.
    speed: int = 30
    # Free-text 5e alignment (e.g. "Chaotic Good"), player-set via character_edit.
    # Pure flavour / DM context, no mechanics.
    alignment: str = ""
    # 5e Inspiration: a single held token the DM grants (via update_character)
    # and the engine spends for advantage on the player's next d20 roll.
    inspiration: bool = False
    stats: dict[str, int] = Field(default_factory=dict)
    # Stacks with quantity + magic_bonus, not plain name strings.
    inventory: list[InventoryItem] = Field(default_factory=list)
    # Which owned item name is currently worn/wielded (None = empty slot).
    equipped_weapon: str | None = None
    equipped_armor: str | None = None
    # Additive on top of equipped_armor's base AC, not a replacement (see _compute_ac).
    equipped_shield: str | None = None
    conditions: list[str] = Field(default_factory=list)
    notes: str = ""
    # Pre-Aetherfall identity, rolled once at creation, never regenerated. Blank on an NPC.
    background: str = ""
    # The paper sheet's "Personal Characteristics". Seeded from the origin
    # table, player-editable via character_edit, read by the DM via
    # character_summary but never written by it. Blank on an NPC.
    personality: str = ""
    ideals: str = ""
    bonds: str = ""
    flaws: str = ""
    xp: int = 0
    level: int = 1
    # Gold pieces. The DM adjudicates loot/rewards/purchases via gold_delta;
    # ponytail: one coin type, add cp/sp/pp if a real coin economy shows up.
    gold: int = 0
    # A stored field, not computed: for a player it's set by _compute_ac
    # (armor + DEX) on creation/equip; for a tracked NPC it's the monster's
    # flat authored value copied from srd.json - two different sources.
    ac: int = 10
    # 5e death saves, player-characters only (an NPC just dies at 0 HP and
    # awards XP). dying is true from HP first hitting 0 until 3 successes
    # (stabilize, hp stays 0) or 3 failures (dead).
    dying: bool = False
    dead: bool = False
    death_save_successes: int = 0
    death_save_failures: int = 0
    # A coarse attitude the DM stays consistent against. Meaningful for a
    # tracked NPC; unused on a player character (shared model).
    disposition: Literal["hostile", "neutral", "friendly"] = "neutral"
    # Stored, not computed: spell_slots is spent/restored constantly and
    # max_spell_slots needs to persist "how many are spent" across a
    # save/reload independent of level. Empty for a non-caster.
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
        """floor((score-10)/2) per ability, in model_dump_json() so neither
        the DM nor the engine recomputes it. Empty when stats is empty."""
        return {key: ability_modifier(score) for key, score in self.stats.items()}

    @computed_field
    @property
    def proficiency_bonus(self) -> int:
        """5e proficiency bonus, a pure function of level. In model_dump_json()."""
        return proficiency_bonus_for_level(self.level)

    def gain_xp(self, amount: int, xp_thresholds: dict[int, int]) -> int:
        """Adds XP and applies every level-up the new total crosses (looped
        - one award can cross several). Returns levels gained. xp_thresholds
        is level -> cumulative-XP (passed in; this module has no rules data).
        No HP growth here - engine.py does that right after, since it needs
        the class hit die."""
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
        """First inventory stack whose name matches (case-insensitive), or None."""
        if not name:
            return None
        normalized = name.strip().lower()
        return next((item for item in self.inventory if item.name.strip().lower() == normalized), None)

    def add_item(self, name: str, magic_bonus: int = 0) -> InventoryItem:
        """Adds one of `name`, stacking onto an entry with the same name AND
        magic_bonus; a different enchantment gets its own stack."""
        for item in self.inventory:
            if item.name.strip().lower() == name.strip().lower() and item.magic_bonus == magic_bonus:
                item.quantity += 1
                return item
        item = InventoryItem(name=name, magic_bonus=magic_bonus)
        self.inventory.append(item)
        return item

    def remove_item(self, name: str) -> bool:
        """Removes one of `name` from the first matching stack, dropping the
        stack at zero. Returns whether a match existed (real removal vs no-op)."""
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

        # Temp HP - a separate buffer real 5e drains before real HP on
        # damage, and that healing never touches. Doesn't stack: a new
        # source takes the higher of the two, not the sum.
        new_temp = update.get("temp_hp")
        if isinstance(new_temp, int) and not isinstance(new_temp, bool) and new_temp > self.temp_hp:
            self.temp_hp = new_temp
            changes.append(f"temp HP now {self.temp_hp}")

        hp_delta = update.get("hp_delta")
        if hp_delta:
            prior_hp = self.hp
            prior_dying = self.dying
            delta = int(hp_delta)
            if delta < 0 and self.temp_hp > 0:
                absorbed = min(self.temp_hp, -delta)
                self.temp_hp -= absorbed
                delta += absorbed
                if absorbed:
                    changes.append(f"{absorbed} absorbed by temp HP ({self.temp_hp} left)")
            self.hp = max(0, min(self.max_hp, self.hp + delta))
            if delta:
                sign = "+" if delta > 0 else ""
                changes.append(f"HP {sign}{delta} (now {self.hp}/{self.max_hp})")

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

        # The engine computes the heal, not the DM. A long rest is a full HP
        # restore (real 5e) and gives back half the hit-dice pool. A short rest
        # spends hit dice one at a time - roll the die + CON, min 0 per die -
        # until full or the pool is empty; a classless character (no hit_die)
        # falls back to the old flat "half of what's missing" stand-in.
        # Deliberately doesn't touch conditions - most SRD conditions don't
        # expire with time, so clearing them here would be a rules error.
        rest = update.get("rest")
        con_mod = self.stat_modifiers.get("con", 0)
        if rest == "long":
            if self.hp < self.max_hp:
                self.hp = self.max_hp
                changes.append(f"long rest: HP restored to {self.hp}/{self.max_hp}")
            regained = min(max(1, self.hit_dice_total // 2), self.hit_dice_total - self.hit_dice_remaining)
            if regained > 0:
                self.hit_dice_remaining += regained
                changes.append(f"long rest: {self.hit_dice_remaining}/{self.hit_dice_total} hit dice")
        elif rest == "short" and self.hp < self.max_hp:
            if self.hit_die and self.hit_dice_remaining > 0:
                spent = healed = 0
                while self.hit_dice_remaining > 0 and self.hp < self.max_hp:
                    _, rolls, _ = dice.roll(self.hit_die)
                    gain = min(max(0, rolls[0] + con_mod), self.max_hp - self.hp)
                    self.hp += gain
                    self.hit_dice_remaining -= 1
                    spent += 1
                    healed += gain
                changes.append(
                    f"short rest: spent {spent} hit {'die' if spent == 1 else 'dice'}, "
                    f"HP +{healed} (now {self.hp}/{self.max_hp}, "
                    f"{self.hit_dice_remaining}/{self.hit_dice_total} hit dice)"
                )
            else:
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

        # Revival (revivify and friends): healing a dead character above 0 HP
        # brings them back. Real 5e's own window (died within the last minute)
        # is DM-adjudicated - no timer is tracked here, so whether a given
        # raise attempt is in time is the DM's call to narrate or refuse.
        if self.hp > 0 and self.dead:
            self.dead = False
            self.dying = False
            self.death_save_successes = 0
            self.death_save_failures = 0
            changes.append(f"{self.name} is brought back from death (now {self.hp}/{self.max_hp})")

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

        gold_delta = update.get("gold_delta")
        if isinstance(gold_delta, int) and not isinstance(gold_delta, bool) and gold_delta:
            applied = max(0, self.gold + gold_delta) - self.gold  # can't go below 0
            self.gold += applied
            changes.append(f"gold {'+' if applied >= 0 else ''}{applied} (now {self.gold})")

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

        # The DM grants Inspiration (a single held token) for playing to the
        # character's traits/ideal/bond/flaw. Only ever set true here - it's
        # spent by the engine on the player's next roll, not by the DM.
        if update.get("inspiration") is True and not self.inspiration:
            self.inspiration = True
            changes.append(f"{self.name} gains Inspiration")

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


class MapNode(BaseModel):
    """Layout hints for one location on the campaign map. The graph itself
    stays in location_map (adjacency) - this only carries the presentation
    layer (docs/protocol.md "Protocol v2 additions - Map"): optional
    coordinates for clients that lay nodes out on a canvas, an emoji icon,
    nothing mechanical. Coordinates are nullable so a client can auto-layout
    any node the DM never placed."""

    name: str
    x: int | None = None
    y: int | None = None
    icon: str = ""


class Clock(BaseModel):
    """A Blades-in-the-Dark-style progress clock - named, segmented tension
    tracker the DM ticks via update_world as events warrant (docs/protocol.md
    "Protocol v2 additions - Clocks"). Server state, not model memory: the
    engine clamps every mutation and narrator_context() surfaces the current
    fill to the DM each turn, so stakes can't silently drift the way they do
    when a prompt is trusted to remember them."""

    name: str
    segments: int
    filled: int = 0

    def tick(self, ticks: int = 1) -> None:
        self.filled = min(self.segments, max(0, self.filled + ticks))


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

    # Map layout hints keyed by location name (MapNode above) - adjacency
    # stays in location_map so old saves keep loading unchanged; this is
    # presentation-only state layered on top of the same graph.
    map_hints: dict[str, MapNode] = Field(default_factory=dict)

    clocks: list[Clock] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def map(self) -> dict:
        """The v2 wire shape (docs/protocol.md "Protocol v2 additions - Map"):
        nodes from location_map's keys merged with any layout hints, edges as
        unique undirected pairs derived from the adjacency lists. Computed,
        not stored - the graph has exactly one source of truth."""
        hints = self.map_hints
        nodes = [
            {
                "name": name,
                "x": hints[name].x if name in hints else None,
                "y": hints[name].y if name in hints else None,
                "icon": hints[name].icon if name in hints else "",
            }
            for name in sorted(set(self.location_map) | set(hints))
        ]
        edges: set[tuple[str, str]] = set()
        for a, neighbors in self.location_map.items():
            for b in neighbors:
                if a != b:
                    edges.add(tuple(sorted((a, b))))
        return {"nodes": nodes, "edges": sorted(list(e) for e in edges)}

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

        # v2 map layout hints (docs/protocol.md "Protocol v2 additions - Map").
        # Upsert by exact name match; a field absent from the entry keeps its
        # current value, an explicit null clears it (so the DM can un-place a
        # node it previously placed). Also creates the node in location_map
        # when new, so hints can never reference a location the graph
        # doesn't have.
        map_nodes = update.get("map_nodes")
        if isinstance(map_nodes, list):
            for entry in map_nodes:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = str(entry["name"])
                node = self.map_hints.get(name)
                if node is None:
                    node = MapNode(name=name)
                    self.map_hints[name] = node
                    changes.append(f"mapped: '{name}'")
                self.location_map.setdefault(name, [])
                if "x" in entry or "y" in entry:
                    node.x = _clamp_int(entry.get("x"), -1000, 1000) if "x" in entry else node.x
                    node.y = _clamp_int(entry.get("y"), -1000, 1000) if "y" in entry else node.y
                    changes.append(f"positioned: '{name}'")
                if "icon" in entry:
                    icon = str(entry["icon"] or "")[:8]
                    if icon != node.icon:
                        node.icon = icon
                        changes.append(f"icon for '{name}': {icon}")

        # v2 progress clocks (docs/protocol.md "Protocol v2 additions -
        # Clocks"). Every mutation clamps server-side; the DM never sets raw
        # state that could escape [0, segments].
        add_clock = update.get("add_clock")
        if isinstance(add_clock, dict) and add_clock.get("name"):
            name = str(add_clock["name"])
            if not any(c.name == name for c in self.clocks):
                segments = _clamp_int(add_clock.get("segments", 6), 2, 12) or 6
                self.clocks.append(Clock(name=name, segments=segments))
                changes.append(f"new clock: '{name}' ({segments} segments)")

        tick_clock = update.get("tick_clock")
        if isinstance(tick_clock, dict) and tick_clock.get("name"):
            name = str(tick_clock["name"])
            ticks = _clamp_int(tick_clock.get("ticks", 1), 1, 12) or 1
            for clock in self.clocks:
                if clock.name == name:
                    was_full = clock.filled >= clock.segments
                    clock.tick(ticks)
                    now_full = clock.filled >= clock.segments
                    verb = (
                        "filled completely"
                        if now_full and not was_full
                        else f"ticked to {clock.filled}/{clock.segments}"
                    )
                    changes.append(f"clock '{name}' {verb}")
                    break

        set_clock = update.get("set_clock")
        if isinstance(set_clock, dict) and set_clock.get("name"):
            name = str(set_clock["name"])
            for clock in self.clocks:
                if clock.name == name:
                    filled = _clamp_int(set_clock.get("filled"), 0, clock.segments)
                    if filled is not None:
                        clock.filled = filled
                        changes.append(f"clock '{name}' set to {clock.filled}/{clock.segments}")
                    break

        remove_clock = update.get("remove_clock")
        if isinstance(remove_clock, str):
            before = len(self.clocks)
            self.clocks = [c for c in self.clocks if c.name != remove_clock]
            if len(self.clocks) < before:
                changes.append(f"removed clock: '{remove_clock}'")

        if not changes:
            return "No changes applied (nothing matched, or all deltas were zero)."
        return "Applied: " + "; ".join(changes) + "."

    def narrator_context(self) -> str:
        """Plain-text current location + summary + active objectives, given
        to the DM on every turn as NarratorBackend.narrate()'s own
        world_summary argument - not left for the model to infer or recall
        from `history` alone. summary was previously computed here-adjacent
        (_resume_recap(), server/engine.py) but only ever reached a
        reconnecting player's own welcome message, never the DM's own
        per-turn context - the rolling history window (Session.history,
        max_history_messages) can scroll past it on an ordinary long
        session exactly as easily as a reconnect gap (ROADMAP.md item 1).
        Same field, same update_world write path, now doing double duty.
        Built to test a specific hypothesis for complete_objective's
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
        if self.summary:
            parts.append(self.summary)
        if self.mood:
            parts.append(f"Current mood: {self.mood}")
        active_objectives = [o.text for o in self.objectives if o.status == "active"]
        if active_objectives:
            parts.append("Active objectives:\n" + "\n".join(f"- {text}" for text in active_objectives))
        if self.clocks:
            parts.append(
                "Progress clocks:\n"
                + "\n".join(f"- {c.name}: {c.filled}/{c.segments}" for c in self.clocks)
            )
        return "\n".join(parts)


MAX_HISTORY_MESSAGES = 12  # 6 player-action/DM-narration exchanges

# The fact ledger's hard size cap. Oldest facts fall off first; the campaign
# summarizer sees the history window, not the ledger, so a fact that ages out
# of the ledger is genuinely gone - hence a generous cap and cheap containment
# dedupe rather than a tight one.
FACT_LEDGER_CAP = 100


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
    # Lobby ready-check: player_ids who have toggled "ready". The adventure
    # auto-starts once every connected player is in here; cleared on start.
    # Persisted so a mid-lobby server restart doesn't silently drop it.
    ready_players: list[str] = Field(default_factory=list)
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

    # World-context selection (docs/protocol.md "Protocol v2 additions -
    # World context -> lorebook"): file NAMES only, persisted so a saved
    # session reloads with its own lore still toggled on. Content never
    # lives here - the engine re-parses the named files into a Lorebook.
    context_files: list[str] = Field(default_factory=list)

    # Rolling campaign summary (docs/REBUILD_PLAN.md): rebuilt by the engine
    # every CAMPAIGN_SUMMARY_INTERVAL resolved turns from the prior summary
    # plus what's about to scroll out of the history window, so a long
    # session's early plot survives the sliding window. Persisted with the
    # session like every other DM-facing state.
    campaign_summary: str = ""
    turns_since_summary: int = 0

    fact_ledger: list[str] = Field(default_factory=list)

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

    def add_facts(self, facts: list[str], cap: int = FACT_LEDGER_CAP) -> int:
        """Append durable session facts to the ledger, deduped and capped.

        A fact is skipped when it's a near-duplicate of one already stored:
        same normalized text, or fully contained in / containing an existing
        entry (the extractor restating "the innkeeper owes you 10 gold" as
        "you are owed 10 gold by the innkeeper" should not grow the ledger).
        Oldest facts fall off past `cap`. Returns how many facts were added.
        """
        added = 0
        for raw in facts:
            text = " ".join(str(raw).split())
            if not text:
                continue
            folded = text.casefold().rstrip(".")
            if any(
                folded in existing or existing in folded
                for existing in (f.casefold().rstrip(".") for f in self.fact_ledger)
            ):
                continue
            self.fact_ledger.append(text)
            added += 1
        if len(self.fact_ledger) > cap:
            self.fact_ledger = self.fact_ledger[-cap:]
        return added
