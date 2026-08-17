from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

from google import genai

from .lore import WorldBible, load_default_world_bible
from .narrator import LOOKUP_RULE_TOOL, UPDATE_CHARACTER_TOOL, ApplyUpdate, RequestRoll, UpdateWorld
from .rules import RulesIndex
from .state import ABILITY_KEYS, SKILL_ABILITIES

GEMINI_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action, in open-ended prose — never as a
numbered or bulleted list of options to choose from. Never break character.

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


def _to_gemini_tool(tool: dict) -> dict:
    """Convert a tool dict (same shape as Anthropic's) to Gemini's function
    calling format. Gemini uses a slightly different schema structure."""
    properties = tool.get("input_schema", {}).get("properties", {})
    required = tool.get("input_schema", {}).get("required", [])

    gemini_properties = {}
    for name, prop in properties.items():
        gemini_prop = {
            "type": prop.get("type", "string").upper(),
            "description": prop.get("description", ""),
        }
        if "enum" in prop:
            gemini_prop["enum"] = prop["enum"]
        gemini_properties[name] = gemini_prop

    return {
        "function_declarations": [{
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": {
                "type": "OBJECT",
                "properties": gemini_properties,
                "required": required,
            },
        }],
    }


GEMINI_TOOLS = [_to_gemini_tool(LOOKUP_RULE_TOOL), _to_gemini_tool(UPDATE_CHARACTER_TOOL)]


class GeminiNarrator:
    """NarratorBackend powered by Google's Gemini API. Free tier available
    (15 RPM, 1M tokens/day). Supports function calling for lookup_rule
    and update_character tools."""

    def __init__(self, api_key: str | None = None):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY required for GeminiNarrator")
        self._client = genai.Client(api_key=api_key)
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

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
        system_prompt = GEMINI_SYSTEM_PROMPT
        if world_summary:
            system_prompt += f"\n\nCampaign state: {world_summary}"

        # Build conversation history for Gemini
        contents = []
        for msg in history:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg.get("content", "")}],
            })

        # Add current action
        contents.append({
            "role": "user",
            "parts": [{"text": f"Character sheet: {character_summary}\n\nPlayer action: {action_text}"}],
        })

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=GEMINI_TOOLS,
                ),
            )

            # Process response
            if response.candidates:
                candidate = response.candidates[0]
                for part in candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        yield part.text
                    elif hasattr(part, "function_call") and part.function_call:
                        # Handle tool calls
                        func_name = part.function_call.name
                        func_args = dict(part.function_call.args) if part.function_call.args else {}

                        if func_name == "lookup_rule":
                            yield f"[Used lookup_rule for {func_args.get('entry_name', 'unknown')}]"
                        elif func_name == "update_character":
                            result = apply_update(func_args)
                            yield f"[{result}]"

        except Exception as e:
            yield f"[Narration error: {e}]"


def create_gemini_narrator() -> GeminiNarrator:
    """Factory function matching the pattern in create_ollama_narrator."""
    return GeminiNarrator(api_key=os.environ.get("GEMINI_API_KEY"))
