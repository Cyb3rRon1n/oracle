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


def ability_modifier(score: int) -> int:
    """The standard 5e ability-modifier formula - floor((score-10)/2).
    A module-level function, not a method, so server/engine.py can apply it
    to a bare score (e.g. computing HP growth from a class's hit die plus a
    CON score) without needing a CharacterSheet instance in hand."""
    return (score - 10) // 2


class CharacterSheet(BaseModel):
    player_id: str
    name: str
    hp: int
    max_hp: int
    character_class: str = ""
    stats: dict[str, int] = Field(default_factory=dict)
    inventory: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    notes: str = ""
    xp: int = 0
    level: int = 1
    # Real 5e's own unarmored baseline (10 + DEX modifier), same fallback a
    # blank/unrecognized class already gets for HP/inventory. A real player
    # character's actual AC (armor + DEX modifier) is computed once by
    # server/engine.py's build_starting_character, which has both the SRD
    # equipment data and the character's own stats in hand; a plain stored
    # field, not a computed one like stat_modifiers, since a real 5e
    # monster's AC (copied verbatim from srd.json onto a tracked NPC) is a
    # flat authored value, not a formula derived from its own stats/gear -
    # the two need genuinely different sources, so one field can't be
    # computed from the sheet alone for both roles. A real, documented
    # simplification: this doesn't recompute if inventory changes later
    # (e.g. a player picks up different armor mid-session) - see
    # docs/protocol.md's "Structured equipment" section.
    ac: int = 10

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

    def apply_update(self, update: dict) -> str:
        """Apply a DM-issued mechanical update (the update_character tool).
        Returns a human-readable summary of what changed, for the tool_result
        the DM sees back."""
        changes: list[str] = []

        hp_delta = update.get("hp_delta")
        if hp_delta:
            self.hp = max(0, min(self.max_hp, self.hp + int(hp_delta)))
            sign = "+" if hp_delta > 0 else ""
            changes.append(f"HP {sign}{hp_delta} (now {self.hp}/{self.max_hp})")

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

        add_item = update.get("add_item")
        if add_item:
            self.inventory.append(add_item)
            changes.append(f"gained '{add_item}'")

        remove_item = update.get("remove_item")
        if remove_item and remove_item in self.inventory:
            self.inventory.remove(remove_item)
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

        if not changes:
            return "No changes applied (nothing matched, or all deltas were zero)."
        return "Applied: " + "; ".join(changes) + "."


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
    flags: dict[str, bool] = Field(default_factory=dict)
    objectives: list[Objective] = Field(default_factory=list)

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

        if not changes:
            return "No changes applied (nothing matched, or all deltas were zero)."
        return "Applied: " + "; ".join(changes) + "."


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
    log: list[dict] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
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
