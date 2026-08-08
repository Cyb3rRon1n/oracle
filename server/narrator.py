from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import anthropic

from .rules import RulesIndex
from .state import ABILITY_KEYS

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
  modifier into dice by hand for that same ability. Use lookup_rule on the acting
  character's class to see which two abilities its saving throws use. For an attack
  roll against a target, use its AC as the dc — the acting character's own AC is in
  their sheet (character_summary's ac); an NPC/monster target's AC is in its stat
  block via lookup_rule. For the damage roll after a hit, use request_roll's weapon
  field (the weapon's name) instead of typing its damage die into dice yourself — the
  engine looks up the real value and reports the damage type too.
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
  recurring character feeling continuous instead of reset each time they appear. When
  the character/NPC rests for a meaningful stretch (camping overnight, resting after a
  fight), use the `rest` field ('short' or 'long') instead of guessing an `hp_delta` -
  the engine computes the real amount healed.
- update_world: call this when something should be remembered for the rest of the
  campaign, not just this scene — a new objective or plot thread emerging, one being
  completed or abandoned, the location changing, or a durable fact about the world. This
  is what a player is actually following across a session; don't call it for passing
  scene detail that doesn't need to persist.
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
            "notes": {
                "type": "string",
                "description": (
                    "A brief standing note about this character/NPC (personality, goal, "
                    "relationship to the party), replacing any previous note. Most useful "
                    "on an NPC's introduction or when the relationship meaningfully changes "
                    "- not needed every call."
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
            "add_objective": {
                "type": "string",
                "description": "A new active objective/plot thread/quest hook to track, in plain language.",
            },
            "complete_objective": {
                "type": "string",
                "description": "The exact text of an existing active objective to mark completed.",
            },
            "remove_objective": {
                "type": "string",
                "description": "The exact text of an existing objective to drop entirely (abandoned, no longer relevant).",
            },
            "set_flag": {
                "type": "string",
                "description": "Name of a world flag to set true (e.g. 'met_the_baron', 'castle_gates_open').",
            },
            "clear_flag": {
                "type": "string",
                "description": "Name of a world flag to clear (set false / remove).",
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
        """


class AnthropicNarrator:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        rules: RulesIndex | None = None,
    ):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._rules = rules or RulesIndex.load_default()

    async def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
    ) -> AsyncIterator[str]:
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [*history, {"role": "user", "content": prompt}]

        for _ in range(MAX_TOOL_ROUNDS):
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=1024,
                system=DM_SYSTEM_PROMPT,
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
