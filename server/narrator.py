from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Protocol

import anthropic

from .rules import RulesIndex

DM_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action. Never break character.

You have two tools available. Use lookup_rule before improvising crunchy mechanics
(monster stats, spell details, class features, equipment, conditions) so numbers stay
consistent from turn to turn. Use web_search sparingly, only for general inspiration or
real-world reference (e.g. period-appropriate detail for a setting) — never to look up
or reproduce copyrighted D&D sourcebook content verbatim. For anything not covered by
lookup_rule, invent original content in the spirit of the genre rather than searching
for or quoting published material."""

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

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

MAX_TOOL_ROUNDS = 4


class NarratorBackend(Protocol):
    def narrate(
        self, world_summary: str, character_summary: str, action_text: str
    ) -> AsyncIterator[str]:
        """Stream narration text in response to a player's action."""


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
        self, world_summary: str, character_summary: str, action_text: str
    ) -> AsyncIterator[str]:
        prompt = (
            f"World state:\n{world_summary or '(session just started)'}\n\n"
            f"Character:\n{character_summary}\n\n"
            f"Player action: {action_text}"
        )
        messages: list[dict] = [{"role": "user", "content": prompt}]

        for _ in range(MAX_TOOL_ROUNDS):
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=1024,
                system=DM_SYSTEM_PROMPT,
                tools=[LOOKUP_RULE_TOOL, WEB_SEARCH_TOOL],
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
                    "content": self._rules.lookup(
                        block.input.get("category", ""), block.input.get("name", "")
                    ),
                }
                for block in response.content
                if block.type == "tool_use" and block.name == "lookup_rule"
            ]
            if not tool_results:
                return
            messages.append({"role": "user", "content": tool_results})


def create_narrator(backend: str | None = None) -> NarratorBackend:
    """Backend selector. Add new NarratorBackend implementations here (e.g. an
    Ollama-backed local narrator) and register them by name to keep the
    engine and transport layers unaware of which one is in use."""
    backend = backend or os.environ.get("DM_BACKEND", "anthropic")
    if backend == "anthropic":
        return AnthropicNarrator(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    raise ValueError(f"Unknown DM_BACKEND {backend!r}. Only 'anthropic' is implemented so far.")
