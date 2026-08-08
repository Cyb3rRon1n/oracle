from __future__ import annotations

import os
from collections.abc import AsyncIterator

import ollama

from .narrator import LOOKUP_RULE_TOOL, UPDATE_CHARACTER_TOOL, ApplyUpdate, RequestRoll, UpdateWorld
from .rules import RulesIndex

OLLAMA_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action, in open-ended prose — never as a
numbered or bulleted list of options to choose from. Never break character.

The acting character's ability scores, real modifiers, and AC are in their sheet
(character_summary's stats/stat_modifiers/ac) - let them inform how you narrate what
the character is good or bad at, and how easy or hard they are to hit, even though you
have no request_roll tool to apply them to mechanically.

You have two tools available:
- lookup_rule: use before improvising crunchy mechanics (monster stats, spell details,
  class features, equipment, conditions) so numbers stay consistent from turn to turn.
- update_character: call this whenever your narration describes something that should
  mechanically change the acting character OR a named NPC/monster — damage, healing,
  gaining or losing an item, or applying/clearing a condition. Narration alone doesn't
  change a sheet; this tool does. Omit target (or use 'self') for the acting character;
  pass an NPC's name as target to introduce or update its own tracked sheet, so its
  wounds and conditions persist turn to turn instead of being forgotten. Call it after
  you've decided the outcome, in the same turn you narrate it. When you introduce a new
  NPC worth remembering, give it a brief notes value too (a sentence on its personality,
  goal, or relationship to the party) — update that note later if the relationship
  changes, so a recurring character feels continuous instead of reset each time they
  appear. Set disposition too when it's clear (hostile/neutral/friendly) — a structured
  value to stay consistent against turn to turn, separate from the free-text notes. When
  the character/NPC rests for a meaningful stretch (camping overnight, resting after a
  fight), use the rest field ('short' or 'long') instead of guessing an hp_delta - the
  engine computes the real amount healed.

For anything not covered by lookup_rule, invent original content in the spirit of the
genre rather than claiming to search for real published material — you have no way to
search the web."""


def _to_ollama_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


OLLAMA_TOOLS = [_to_ollama_tool(LOOKUP_RULE_TOOL), _to_ollama_tool(UPDATE_CHARACTER_TOOL)]

MAX_TOOL_ROUNDS = 4


class OllamaNarrator:
    """Local NarratorBackend backed by Ollama. No web_search — that's an
    Anthropic-hosted server tool with no local equivalent, so this backend
    leans on lookup_rule and the model's own judgment instead."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str | None = None,
        rules: RulesIndex | None = None,
    ):
        self._client = ollama.AsyncClient(host=host)
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
        # request_roll/update_world are accepted for NarratorBackend interface
        # parity but deliberately unused: neither is in OLLAMA_TOOLS, so local
        # models can't call them yet. See ROADMAP.md item 6 - this session's
        # investigation found small local models already miss the one
        # existing tool on most clearly-warranted turns; adding more required
        # tool calls before narration would only compound that. Scoped to
        # AnthropicNarrator first; local support is a deliberate follow-up,
        # not an oversight.
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": prompt},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            stream = await self._client.chat(
                model=self._model,
                messages=messages,
                tools=OLLAMA_TOOLS,
                stream=True,
            )

            tool_calls = []
            async for chunk in stream:
                if chunk.message.content:
                    yield chunk.message.content
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)

            if not tool_calls:
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                if tc.function.name == "lookup_rule":
                    result = self._rules.lookup(
                        tc.function.arguments.get("category", ""),
                        tc.function.arguments.get("name", ""),
                    )
                elif tc.function.name == "update_character":
                    result = apply_update(dict(tc.function.arguments))
                else:
                    continue
                messages.append({"role": "tool", "content": result})


def create_ollama_narrator() -> OllamaNarrator:
    return OllamaNarrator(
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        host=os.environ.get("OLLAMA_HOST"),
    )
