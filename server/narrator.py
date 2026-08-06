from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import anthropic

from .rules import RulesIndex

DM_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action. Never break character.

You have three tools available:
- lookup_rule: use before improvising crunchy mechanics (monster stats, spell details,
  class features, equipment, conditions) so numbers stay consistent from turn to turn.
- update_character: call this whenever your narration describes something that should
  mechanically change the acting character — damage, healing, gaining or losing an item,
  or applying/clearing a condition. Narration alone doesn't change the sheet; this tool
  does. Call it after you've decided the outcome, in the same turn you narrate it.
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
        "Apply a mechanical change to the acting character's sheet as a result of "
        "narrated events — damage, healing, gaining or losing an item, or a new or "
        "cleared condition. All fields are optional; include only what actually "
        "changed. This only affects the character taking their turn right now."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hp_delta": {
                "type": "integer",
                "description": "Change in hit points. Negative for damage, positive for healing.",
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
        },
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

MAX_TOOL_ROUNDS = 4

ApplyUpdate = Callable[[dict], str]


class NarratorBackend(Protocol):
    def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
    ) -> AsyncIterator[str]:
        """Stream narration text in response to a player's action.

        `history` is the rolling window of prior turns as plain
        {"role": "user"/"assistant", "content": str} messages — the DM's own
        past narration, not the tool calls it made to produce them.

        `apply_update` is called with the update_character tool's input
        whenever the DM decides a narrated outcome should mechanically
        change the acting character's sheet; it returns a description of
        what changed, which becomes that tool call's result.
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
    ) -> AsyncIterator[str]:
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [*history, {"role": "user", "content": prompt}]

        for _ in range(MAX_TOOL_ROUNDS):
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=1024,
                system=DM_SYSTEM_PROMPT,
                tools=[LOOKUP_RULE_TOOL, UPDATE_CHARACTER_TOOL, WEB_SEARCH_TOOL],
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
                    "content": self._run_tool(block, apply_update),
                }
                for block in response.content
                if block.type == "tool_use" and block.name in ("lookup_rule", "update_character")
            ]
            if not tool_results:
                return
            messages.append({"role": "user", "content": tool_results})

    def _run_tool(self, block, apply_update: ApplyUpdate) -> str:
        if block.name == "lookup_rule":
            return self._rules.lookup(
                block.input.get("category", ""), block.input.get("name", "")
            )
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
