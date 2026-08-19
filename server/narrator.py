from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import anthropic

from .lore import WorldBible, load_default_world_bible
from .rules import RulesIndex
from .state import ABILITY_KEYS, SKILL_ABILITIES

DM_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action, in open-ended prose — never as a
numbered or bulleted list of options to choose from. Never break character.

You have five tools available:
- request_roll: call this BEFORE narrating the outcome of an action whose success is
  genuinely uncertain — an attack, a skill check, a saving throw. Don't call it for
  actions with an obvious, certain outcome. It returns the roll, and a success/failure
  verdict if you gave it a dc. Narrate the outcome to match what it returns — don't
  decide success or failure yourself and then narrate a roll that would contradict it.
  The acting character's ability scores and their real modifiers are already in their
  sheet (character_summary's stats/stat_modifiers) — when a roll is tied to one of
  those abilities, pass its key (str/dex/con/int/wis/cha) as request_roll's ability
  field and the engine adds the correct modifier itself; don't also compute and type a
  modifier into dice by hand for that same ability. For a skill check specifically
  (e.g. sneaking, spotting something, persuading someone), pass request_roll's skill
  field (the real 5e skill name, like stealth or perception) instead of ability — the
  engine resolves the right ability *and* adds the character's real proficiency bonus
  automatically if they're proficient in it, which you have no way to know or compute
  yourself. Use lookup_rule on the acting character's class to see which two abilities
  its saving throws use. For an attack
  roll against a target, use its AC as the dc — the acting character's own AC is in
  their sheet (character_summary's ac); an NPC/monster target's AC is in its stat
  block via lookup_rule. For the damage roll after a hit, use request_roll's weapon
  field (the weapon's name) instead of typing its damage die into dice yourself — the
  engine looks up the real value and reports the damage type too. For a spell attack
  (fire_bolt, ray_of_frost), use request_roll's spell field the same way — real damage
  die/type, ability, and proficiency all resolved for you. A spell that instead forces
  a saving throw (sacred_flame, fireball) has no roll of your own to make — narrate
  against the character's own spell_save_dc (in their sheet) instead, and cast it via
  update_character's cast_spell field, not request_roll. Pass roll_kind
  (attack/save/check) so the engine can apply real per-condition rules correctly —
  poisoned/frightened don't affect saving throws, and prone only affects attack rolls;
  without roll_kind the engine can't tell these apart and applies disadvantage more
  broadly than the real rules do. If the acting character is poisoned, frightened, or
  prone and the current roll_kind is affected, the engine automatically rolls that
  request_roll with disadvantage (2d20, worse kept) — you don't need to ask for this or
  account for it yourself, but do narrate the result you get back, which may come out
  lower than you'd expect for that reason.
- lookup_rule: use before improvising crunchy mechanics (monster stats, spell details,
  class features, equipment, conditions) so numbers stay consistent from turn to turn.
- update_character: call this whenever your narration describes something that should
  mechanically change the acting character OR a named NPC/monster — damage, healing,
  gaining or losing an item, or applying/clearing a condition. Narration alone doesn't
  change a sheet; this tool does. Omit target (or use 'self') for the acting character;
  pass an NPC's name as target to introduce or update its own tracked sheet, so its
  wounds and conditions persist turn to turn instead of being forgotten. Call it after
  you've decided the outcome (including after a request_roll result, if one was needed),
  in the same turn you narrate it. When you introduce a new NPC worth remembering, give
  it a brief `notes` value too (a sentence on its personality, goal, or relationship to
  the party) — update that note later if the relationship changes. This is what keeps a
  recurring character feeling continuous instead of reset each time they appear. Set
  `disposition` too when it's clear (hostile/neutral/friendly) — a structured value to
  stay consistent against turn to turn, separate from the free-text `notes`. When
  the character/NPC rests for a meaningful stretch (camping overnight, resting after a
  fight), use the `rest` field ('short' or 'long') instead of guessing an `hp_delta` -
  the engine computes the real amount healed. For a wizard/cleric casting one of their
  own known spells (character_summary's known_spells), use `cast_spell` (the spell's
  name) — the engine deducts the real spell slot itself (or tells you if they don't
  know it or have none left of that level); cantrips cost no slot. This only tracks
  the slot spent — still narrate the spell's actual effect and apply it yourself with
  this same call's other fields (hp_delta, add_condition, ...) or a request_roll.
- update_world: call this when something should be remembered for the rest of the
  campaign, not just this scene — a new objective or plot thread emerging, one being
  resolved, the location changing, or a durable fact about the world. This is what a
  player is actually following across a session; don't call it for passing scene detail
  that doesn't need to persist. An objective can resolve three real ways, not just one:
  complete_objective for a real success, expire_objective when it goes stale on its own
  (a missed window, events moving on), or fail_objective when the party's own actions
  genuinely fell short — use whichever one actually happened rather than defaulting to
  complete_objective or silently leaving it active forever.
- web_search: use sparingly, only for general inspiration or real-world reference (e.g.
  period-appropriate detail for a setting) — never to look up or reproduce copyrighted
  D&D sourcebook content verbatim. For anything not covered by lookup_rule, invent
  original content in the spirit of the genre rather than searching for published
  material."""

LOOKUP_RULE_TOOL = {
    "name": "lookup_rule",
    "description": (
        "Look up official D&D 5e SRD data by name: a monster stat block, a spell's "
        "details, a class's core level-1 features, a piece of equipment, or a "
        "condition's rules text. Use this before improvising crunchy rules details "
        "(HP, AC, damage dice, spell level/range/duration, etc.) so mechanics stay "
        "consistent. Returns 'not found' if there's no local match — for anything "
        "not found, use your own judgment to invent something original rather than "
        "searching the web for copyrighted sourcebook content."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["monster", "spell", "class", "equipment", "condition"],
            },
            "name": {
                "type": "string",
                "description": "Name to look up, e.g. 'goblin', 'fireball', 'poisoned'",
            },
        },
        "required": ["category", "name"],
    },
}

UPDATE_CHARACTER_TOOL = {
    "name": "update_character",
    "description": (
        "Apply a mechanical change to a sheet as a result of narrated events — "
        "damage, healing, gaining or losing an item, or a new or cleared condition. "
        "All fields are optional; include only what actually changed. Omit target "
        "(or use 'self') for the acting character. For an NPC or monster, pass its "
        "name as target instead: the first call for a given name creates a tracked "
        "sheet for it (set max_hp to its real max HP from lookup_rule if you know "
        "it), and every later call with that same name updates the same tracked "
        "NPC, so its wounds and conditions persist turn to turn instead of being "
        "forgotten."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Who this update applies to. Omit or use 'self' for the acting "
                    "character. Otherwise, the name of an NPC/monster in the scene — "
                    "creates a new tracked NPC on first use, updates it afterward."
                ),
            },
            "max_hp": {
                "type": "integer",
                "description": (
                    "Starting/maximum HP for a new NPC target, from lookup_rule if "
                    "possible. Only meaningful the first time that name is used; "
                    "ignored for 'self' and for an NPC already being tracked."
                ),
            },
            "hp_delta": {
                "type": "integer",
                "description": "Change in hit points. Negative for damage, positive for healing.",
            },
            "rest": {
                "type": "string",
                "enum": ["short", "long"],
                "description": (
                    "Use when the character/NPC rests for a meaningful stretch of time "
                    "(camping overnight, resting after a fight) instead of guessing an "
                    "hp_delta yourself - the engine computes the real amount healed. "
                    "'long' fully restores HP; 'short' restores about half of what's "
                    "currently missing. Don't combine with hp_delta in the same call."
                ),
            },
            "add_item": {"type": "string", "description": "Item name to add to inventory."},
            "magic_bonus": {
                "type": "integer",
                "description": (
                    "Only meaningful together with add_item, for a real magic weapon/armor/"
                    "shield you're narrating the character finding or receiving (e.g. a +1 "
                    "longsword). The flat bonus it grants - added to that item's real attack/"
                    "damage or AC automatically. Omit for an ordinary, non-magical item."
                ),
            },
            "remove_item": {
                "type": "string",
                "description": "Item name to remove from inventory, if present.",
            },
            "add_condition": {
                "type": "string",
                "description": "Condition to apply, e.g. 'poisoned', 'prone'.",
            },
            "remove_condition": {
                "type": "string",
                "description": "Condition to clear, if present.",
            },
            "cast_spell": {
                "type": "string",
                "description": (
                    "The acting character casts one of their own known spells (e.g. "
                    "'fire bolt', 'cure wounds') - the engine deducts the real spell "
                    "slot automatically (nothing to compute yourself), or refuses if "
                    "they don't know it or have no slot left of the right level. "
                    "Cantrips (fire_bolt, ray_of_frost, sacred_flame, guidance) cost no "
                    "slot at all. This only tracks the slot being spent - narrate the "
                    "spell's actual effect yourself and, if it deals damage, heals, or "
                    "changes a condition, still apply that separately with this same "
                    "call's other fields (hp_delta, add_condition, ...) or a following "
                    "request_roll. Only self can cast - omit target, or this is ignored "
                    "for an NPC."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "A brief standing note about this character/NPC (personality, goal, "
                    "relationship to the party), replacing any previous note. Most useful "
                    "on an NPC's introduction or when the relationship meaningfully changes "
                    "- not needed every call."
                ),
            },
            "disposition": {
                "type": "string",
                "enum": ["hostile", "neutral", "friendly"],
                "description": (
                    "An NPC/monster's current attitude toward the party. Set this on "
                    "introduction if it's clear from context, and update it later if the "
                    "relationship meaningfully changes (e.g. a fight ends and they surrender, "
                    "or a favor is repaid). Not meaningful for 'self' - omit for the acting "
                    "character."
                ),
            },
        },
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

UPDATE_WORLD_TOOL = {
    "name": "update_world",
    "description": (
        "Update the campaign's persistent world state - use this when the scene changes "
        "location, a new objective/plot thread emerges, an existing one is completed or "
        "abandoned, or a durable fact about the world changes. This is what keeps the "
        "story coherent beyond the immediate conversation - call it when something should "
        "be remembered for the rest of the campaign, not for passing scene detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Update the current location, if it changed."},
            "summary": {
                "type": "string",
                "description": (
                    "A short (1-3 sentence) standing summary of the campaign's overall "
                    "situation so far, replacing the previous one. Update this when the "
                    "big picture changes, not every turn."
                ),
            },
            "mood": {
                "type": "string",
                "description": (
                    "The current scene's mood/environment tag - a short descriptor of the "
                    "emotional tone of the present moment, e.g. 'tense', 'foreboding', "
                    "'hopeful', 'festive', 'oppressive'. Update it when the tone of the "
                    "current scene meaningfully shifts (entering a haunted ruin, the mood "
                    "lifting after a victory), not every turn."
                ),
            },
            "add_objective": {
                "type": "string",
                "description": "A new active objective/plot thread/quest hook to track, in plain language.",
            },
            "complete_objective": {
                "type": "string",
                "description": "The exact text of an existing active objective to mark completed.",
            },
            "expire_objective": {
                "type": "string",
                "description": (
                    "The exact text of an existing active objective that's gone stale on its own - "
                    "e.g. a time-limited opportunity passed, or events moved on without the party "
                    "acting. Distinct from failed (below): nothing anyone did caused this."
                ),
            },
            "fail_objective": {
                "type": "string",
                "description": (
                    "The exact text of an existing active objective the party genuinely failed - "
                    "e.g. the target escaped after a fight, or a required condition was violated. "
                    "Distinct from expired (above): this reflects an outcome, not a missed window."
                ),
            },
            "remove_objective": {
                "type": "string",
                "description": (
                    "The exact text of an existing objective to drop entirely, not just mark expired "
                    "or failed - use this for tracking mistakes or an objective that turned out to be "
                    "irrelevant, not for a real story outcome the player should see reflected."
                ),
            },
            "set_flag": {
                "type": "string",
                "description": "Name of a world flag to set true (e.g. 'met_the_baron', 'castle_gates_open').",
            },
            "clear_flag": {
                "type": "string",
                "description": "Name of a world flag to clear (set false / remove).",
            },
            "add_location": {
                "type": "string",
                "description": (
                    "Register a new location on the party's map, even before any exits to or "
                    "from it are known yet - use this the moment a new place is discovered."
                ),
            },
            "connect_locations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Two location names to connect with a passage or exit, e.g. "
                    "['Great Hall', 'Armory'] - adds both to the map if either is new, and "
                    "records a two-way connection between them. Call this whenever the party "
                    "discovers or moves through a connection between two places, so a return "
                    "visit's layout stays consistent instead of being reinvented each time."
                ),
            },
        },
    },
}

REQUEST_ROLL_TOOL = {
    "name": "request_roll",
    "description": (
        "Roll dice to resolve an action whose outcome is genuinely uncertain - an "
        "attack, a skill check, a saving throw, or similar. Call this before narrating "
        "the outcome, then narrate the result to match what's returned: a success/"
        "failure verdict if dc was given, or just the rolled total otherwise (e.g. for "
        "a damage roll with no pass/fail threshold). Don't call this for actions with "
        "an obvious, certain outcome - only when success is genuinely in doubt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dice": {
                "type": "string",
                "description": "Dice notation, e.g. '1d20+3' for an attack or ability check.",
            },
            "dc": {
                "type": "integer",
                "description": (
                    "The difficulty class the roll must meet or beat to succeed. Omit "
                    "for a roll with no pass/fail threshold, e.g. a damage roll."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Brief description of what's being attempted, e.g. 'attack roll vs the goblin'.",
            },
            "ability": {
                "type": "string",
                "enum": list(ABILITY_KEYS),
                "description": (
                    "If this roll is based on one of the acting character's own ability "
                    "scores (an ability check, a saving throw, most attacks), name it here "
                    "and the engine adds the real modifier automatically - don't also type "
                    "a modifier into dice yourself for this same ability, or it'll be "
                    "applied twice. The character's scores and modifiers are in their sheet "
                    "(character_summary's stats/stat_modifiers). Omit for a roll that isn't "
                    "tied to any ability."
                ),
            },
            "weapon": {
                "type": "string",
                "description": (
                    "For a damage roll: the weapon's name (e.g. 'longsword') - the engine "
                    "looks up its real SRD damage die and uses that instead of whatever's "
                    "in dice, and reports the damage type too. Combine with ability (the "
                    "character's STR for most melee weapons, DEX for ranged/finesse ones) "
                    "the same way real damage rolls add an ability modifier on top of the "
                    "weapon's own die. Omit for an attack roll or check - this is for the "
                    "follow-up damage roll after a hit, not the roll to see if it lands."
                ),
            },
            "skill": {
                "type": "string",
                "enum": sorted(SKILL_ABILITIES),
                "description": (
                    "For a skill check: the real 5e skill name (e.g. 'stealth', "
                    "'perception'). The engine resolves its real governing ability and "
                    "adds that modifier automatically - don't also pass ability for the "
                    "same roll unless you specifically want a different ability applied. "
                    "Also automatically adds the character's real proficiency bonus if "
                    "they're proficient in this skill - you don't need to know or track "
                    "which skills a character is proficient in. Omit for a roll that isn't "
                    "a skill check (an attack, a saving throw, a raw damage roll)."
                ),
            },
            "spell": {
                "type": "string",
                "description": (
                    "For a spell attack roll (e.g. 'fire bolt', 'sacred flame'): the "
                    "engine resolves the spell's real damage die/type and adds the "
                    "character's spellcasting ability modifier plus proficiency bonus "
                    "automatically (a spell attack always gets proficiency, unlike a "
                    "skill check). Only applies to attack-roll spells - a spell that "
                    "instead forces a saving throw (e.g. sacred_flame, fireball) has no "
                    "roll of your own to make here; use the character's own spell_save_dc "
                    "(in their sheet) and narrate whether the target's save succeeds "
                    "instead. Doesn't consume a spell slot - pair with a separate "
                    "cast_spell on update_character for that."
                ),
            },
            "roll_kind": {
                "type": "string",
                "enum": ["attack", "save", "check"],
                "description": (
                    "What kind of roll this is - an attack roll, a saving throw, or an "
                    "ability check. Naming a skill above already implies 'check' "
                    "automatically - only set this yourself for a roll with no skill "
                    "(a raw ability check, an attack, a save). Doesn't change the roll's "
                    "own math, but some tracked "
                    "conditions only affect certain roll kinds under the real rules (e.g. "
                    "poisoned/frightened don't affect saving throws, prone only affects "
                    "attack rolls) - the engine applies that automatically when this is "
                    "given. Omit for a roll this doesn't cleanly apply to, e.g. a damage "
                    "roll after a hit already landed."
                ),
            },
        },
        "required": ["dice"],
    },
}

MAX_TOOL_ROUNDS = 4

ApplyUpdate = Callable[[dict], str]
RequestRoll = Callable[[dict], str]
UpdateWorld = Callable[[dict], str]


class NarratorBackend(Protocol):
    def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll,
        update_world: UpdateWorld,
        world_summary: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream narration text in response to a player's action.

        `history` is the rolling window of prior turns as plain
        {"role": "user"/"assistant", "content": str} messages — the DM's own
        past narration, not the tool calls it made to produce them.

        `apply_update` is called with the update_character tool's input
        whenever the DM decides a narrated outcome should mechanically
        change a sheet — the acting character's own, or a named NPC's
        (via the input's `target` field); it returns a description of
        what changed, which becomes that tool call's result.

        `request_roll` is called with the request_roll tool's input whenever
        the DM needs to resolve a genuinely uncertain action; it returns the
        roll result (and success/failure verdict, if a dc was given) as that
        tool call's result, for the DM to narrate against.

        `update_world` is called with the update_world tool's input whenever
        the DM decides something should persist beyond the current scene —
        a new/completed objective, a location change, a world flag; it
        returns a description of what changed, which becomes that tool
        call's result.

        `world_summary` is the current location and active objectives
        (WorldState.narrator_context(), server/state.py), given directly
        rather than left for the DM to infer from `history` alone. Built
        to test whether completing an objective (update_world's
        complete_objective, matched by exact text) would go more reliably
        if the DM could copy that text directly instead of recalling it
        from several turns back - real --repeat testing found that wasn't
        the actual bottleneck (see server/narrator_ollama.py's
        WORLD_UPDATE_PROMPT_ADDENDUM comment for the full writeup), so
        this is kept as real, defensible grounding for the DM rather than
        a proven fix for that specific gap. Empty/None when there's
        nothing to report yet (a fresh session) or the caller has no
        world-tracking to offer.
        """

    async def check_missed_change(
        self, narration: str, character_summary: str, apply_update: ApplyUpdate
    ) -> bool:
        """Optional - not every backend needs to implement this (checked via
        getattr at the call site, server/engine.py's `_narrate_and_apply`).

        A narrower follow-up check, not a full turn: called only when
        `POSSIBLE_UNTRACKED_CHANGE_PATTERN` matches a turn's narration with
        no real `apply_update` call already made this turn (server/
        engine.py) - gives the DM one real chance to self-correct with a
        genuine tool call before falling back to the passive "may be out
        of sync" warning, rather than trying to parse a specific number out
        of `narration`'s own prose (this project's own "the engine/model
        decides via a real mechanism, never guessed from text" convention,
        applied here the same way it already governs ability scores, XP,
        and damage rolls elsewhere). Returns whether a correction was
        actually applied - server/engine.py branches its own follow-up
        messaging on this.
        """

    async def propose_correction(self, narration: str, character_summary: str) -> dict | None:
        """Optional - not every backend needs this (getattr at the call
        site, server/engine.py).

        Called right before the passive missed-change advisory is sent -
        after check_missed_change already declined to auto-correct. Returns
        a best-guess update_character-shaped dict (target/hp_delta/
        add_condition) the player can confirm and apply via /apply, or None
        when there's genuinely nothing to propose. Deliberately framed as a
        hypothesis for the player to weigh against what they actually saw,
        not a decision the model is confident in - it already decided not to
        act. Never a regex parse of narration text, the same
        "the engine/model decides via a real mechanism" convention
        check_missed_change documents above.
        """
        return None


class AnthropicNarrator:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        rules: RulesIndex | None = None,
        world_bible: WorldBible | None = None,
    ):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._rules = rules or RulesIndex.load_default()
        # Computed once, not per-call - present on every narrate() call
        # regardless of the rolling history window's size, so the world's
        # own facts (server/lore/__init__.py's WorldBible) can't scroll out
        # of context and drift or get reinvented inconsistently over a
        # long session.
        self._system_prompt = DM_SYSTEM_PROMPT + (world_bible or load_default_world_bible()).system_prompt_block()

    async def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
        world_summary: str | None = None,
    ) -> AsyncIterator[str]:
        prompt = f"Character:\n{character_summary}\n\n"
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        prompt += f"Player action: {action_text}"
        messages: list[dict] = [*history, {"role": "user", "content": prompt}]

        for _ in range(MAX_TOOL_ROUNDS):
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=1024,
                system=self._system_prompt,
                tools=[
                    REQUEST_ROLL_TOOL,
                    LOOKUP_RULE_TOOL,
                    UPDATE_CHARACTER_TOOL,
                    UPDATE_WORLD_TOOL,
                    WEB_SEARCH_TOOL,
                ],
                messages=messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    yield chunk
                response = await stream.get_final_message()

            if response.stop_reason != "tool_use":
                return

            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": self._run_tool(block, apply_update, request_roll, update_world),
                }
                for block in response.content
                if block.type == "tool_use"
                and block.name in ("lookup_rule", "update_character", "request_roll", "update_world")
            ]
            if not tool_results:
                return
            messages.append({"role": "user", "content": tool_results})

    async def check_missed_change(
        self, narration: str, character_summary: str, apply_update: ApplyUpdate
    ) -> bool:
        """See NarratorBackend.check_missed_change's own docstring for the
        full "why" - a real, separate, non-streamed call, not part of
        narrate()'s own tool-round loop above. Only offers update_character
        (not request_roll/lookup_rule/update_world) - this is a correction
        check on narration that already happened, not a new narrative turn,
        so nothing else is relevant. `max_tokens` is small since the only
        useful response is a tool call or nothing; a bare text reply (the
        model deciding not to correct anything) is deliberately discarded,
        never shown to the player - the real narration already streamed."""
        prompt = (
            f"Character:\n{character_summary}\n\n"
            f"You just narrated this, but didn't call update_character:\n{narration}\n\n"
            "Review it: if it describes a real change to a character's or NPC's hp, "
            "inventory, or conditions that should have been recorded, call "
            "update_character now with the correct target and fields. If nothing "
            "actually needs correcting, don't call anything."
        )
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=256,
            system=self._system_prompt,
            tools=[UPDATE_CHARACTER_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason != "tool_use":
            return False
        corrected = False
        for block in response.content:
            if block.type == "tool_use" and block.name == "update_character":
                apply_update(block.input)
                corrected = True
        return corrected

    def _run_tool(
        self,
        block,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None,
        update_world: UpdateWorld | None,
    ) -> str:
        if block.name == "lookup_rule":
            return self._rules.lookup(
                block.input.get("category", ""), block.input.get("name", "")
            )
        if block.name == "request_roll":
            if request_roll is None:
                return "Rolling isn't available right now."
            return request_roll(block.input)
        if block.name == "update_world":
            if update_world is None:
                return "World-state tracking isn't available right now."
            return update_world(block.input)
        return apply_update(block.input)


def create_narrator(backend: str | None = None) -> NarratorBackend:
    """Backend selector — keeps the engine and transport layers unaware of
    which backend is in use."""
    backend = backend or os.environ.get("DM_BACKEND", "anthropic")
    if backend == "anthropic":
        return AnthropicNarrator(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    if backend == "ollama":
        from .narrator_ollama import create_ollama_narrator  # optional dependency

        return create_ollama_narrator()
    raise ValueError(f"Unknown DM_BACKEND {backend!r}. Valid backends: 'anthropic', 'ollama'.")
