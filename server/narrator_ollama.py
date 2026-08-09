from __future__ import annotations

import json
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

# The default path (see structured_output below), not merely an
# experiment anymore - a live, 5-repeat qwen2.5:7b comparison against the
# harness's own scenario found this roughly doubles real tool-call
# correctness over native tool-calling (66% vs 29% pooled), the first real
# improvement across six prior experiments in ROADMAP.md item 6's tool-call
# reliability investigation, all of which plateaued around 29% regardless
# of model, scale, or prompt changes. The mechanism: instead of asking the
# model to *decide* whether to invoke a separate tool (a repeatedly-missed
# step across qwen2.5/llama3.1/qwen3), the entire response is constrained
# to a JSON schema that always has a mechanical_change field to fill in -
# "should I call this?" becomes "fill in this field", a different problem
# shape small models handle more reliably than tool-selection, evidenced
# by real tool-call *attempts* jumping from 0-2/8 to 6-7/8 turns per run.
# Deliberately a minimal first slice, not full update_character parity:
# no lookup_rule support (the reliability harness's own scoring never
# exercises it), no rest/notes/disposition/cast_spell fields - just enough
# (target, hp_delta, add_condition) to validate the core hypothesis against
# the harness's existing scenario, which only ever needs those three. Real
# production sessions still lose access to those other update_character
# fields while this is active - a real, known gap, not silently accepted;
# see ROADMAP.md for what expanding schema parity would need.
STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {
            "type": "string",
            "description": "The narrated outcome, in character, 3-5 sentences, open-ended prose.",
        },
        "mechanical_change": {
            "type": "boolean",
            "description": "True if this turn's outcome changes anyone's HP, inventory, or conditions.",
        },
        "target": {
            "type": "string",
            "description": (
                "Who the mechanical change applies to - 'self' for the acting character, or "
                "an NPC's name (whoever actually got hurt or changed, not necessarily who "
                "acted). Only meaningful when mechanical_change is true."
            ),
        },
        "hp_delta": {
            "type": "integer",
            "description": "HP change - negative for damage, positive for healing. 0 if not applicable.",
        },
        "add_condition": {
            "type": "string",
            "description": "A condition to apply (e.g. 'poisoned'), or an empty string if none.",
        },
    },
    "required": ["narration", "mechanical_change"],
}

STRUCTURED_OUTPUT_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Respond with a single JSON object matching the given schema - never prose outside that JSON,
never a tool call. `narration` is your in-character response (3-5 sentences, open-ended prose,
never a numbered or bulleted list of options). Set `mechanical_change` to true whenever the
narration describes something that should change a sheet - damage, healing, gaining or losing
an item, or a new/cleared condition - and fill in `target`/`hp_delta`/`add_condition`
accordingly. `target` is whoever actually got hurt or changed, not simply whoever acted - if
the acting character attacks someone else and that other creature takes the damage, target is
that NPC's name, never the acting character's own name or 'self'. Set mechanical_change to
false (and leave the other fields at their defaults) for a turn with no real mechanical
outcome. Never break character in `narration`."""


class OllamaNarrator:
    """Local NarratorBackend backed by Ollama. No web_search — that's an
    Anthropic-hosted server tool with no local equivalent, so this backend
    leans on lookup_rule and the model's own judgment instead."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str | None = None,
        rules: RulesIndex | None = None,
        structured_output: bool = True,
    ):
        self._client = ollama.AsyncClient(host=host)
        self._model = model
        self._rules = rules or RulesIndex.load_default()
        # Defaults on (see STRUCTURED_OUTPUT_SCHEMA above for why) - a
        # real constructor flag rather than a separate class, since every
        # other piece of state (client/model/rules) is identical either
        # way and this project already has precedent (max_history_messages,
        # scripts/live_reliability_check.py's --repeat) for exposing a
        # real behavioral knob as a plain parameter rather than a subclass.
        # False (or OLLAMA_STRUCTURED_OUTPUT=0, create_ollama_narrator's
        # own env var) is a real escape hatch to the legacy path, not a
        # dead option - a session missing rest/notes/disposition/
        # cast_spell might prefer full update_character parity over the
        # higher correctness rate on the fields structured mode does cover.
        self._structured_output = structured_output

    def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
    ) -> AsyncIterator[str]:
        if self._structured_output:
            return self._narrate_structured(history, character_summary, action_text, apply_update)
        return self._narrate_tool_calling(history, character_summary, action_text, apply_update)

    async def _narrate_structured(
        self, history: list[dict], character_summary: str, action_text: str, apply_update: ApplyUpdate
    ) -> AsyncIterator[str]:
        """The experimental structured-output path (see
        STRUCTURED_OUTPUT_SCHEMA above) - constrains the entire response to
        JSON via Ollama's format parameter instead of native tool-calling.
        Not streamed: constrained generation doesn't produce meaningfully
        parseable partial JSON chunk-by-chunk the way free-form tool-calling
        text does, so this yields the full narration as one chunk once the
        complete response is in hand - `_narrate_and_apply`'s own buffering
        (`buffer += chunk`) handles a single big chunk exactly the same as
        many small ones."""
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": STRUCTURED_OUTPUT_SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": prompt},
        ]
        response = await self._client.chat(
            model=self._model, messages=messages, format=STRUCTURED_OUTPUT_SCHEMA, stream=False
        )

        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            # A real, possible failure mode for constrained generation on a
            # small model - malformed JSON despite the schema constraint.
            # Surfaces the raw content as narration rather than silently
            # losing the turn, the same "don't hide a real failure" spirit
            # _on_player_action's own exception handling already has.
            yield response.message.content or ""
            return

        yield data.get("narration", "")

        if data.get("mechanical_change"):
            update = {"target": data.get("target") or "self"}
            if data.get("hp_delta"):
                update["hp_delta"] = data["hp_delta"]
            if data.get("add_condition"):
                update["add_condition"] = data["add_condition"]
            apply_update(update)

    async def _narrate_tool_calling(
        self, history: list[dict], character_summary: str, action_text: str, apply_update: ApplyUpdate
    ) -> AsyncIterator[str]:
        # request_roll/update_world aren't parameters here (unlike the outer
        # narrate() this is dispatched from, which keeps them for
        # NarratorBackend interface parity) - neither is in OLLAMA_TOOLS, so
        # local models can't call them yet. See ROADMAP.md item 6 - this
        # session's investigation found small local models already miss the
        # one existing tool on most clearly-warranted turns; adding more
        # required tool calls before narration would only compound that.
        # Scoped to AnthropicNarrator first; local support is a deliberate
        # follow-up, not an oversight.
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
    # OLLAMA_STRUCTURED_OUTPUT defaults on, matching OllamaNarrator's own
    # default - a real escape hatch to the legacy native-tool-calling path
    # (set to "0"/"false"/"no"), not required for normal use. See
    # STRUCTURED_OUTPUT_SCHEMA's own docstring and ROADMAP.md for why this
    # is the default: a live, 5-repeat qwen2.5:7b comparison found it
    # roughly doubles real tool-call correctness (66% vs 29% pooled) over
    # native tool-calling, the first real improvement across six
    # experiments in this project's own tool-call reliability investigation.
    structured = os.environ.get("OLLAMA_STRUCTURED_OUTPUT", "true").strip().lower() not in ("0", "false", "no")
    return OllamaNarrator(
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        host=os.environ.get("OLLAMA_HOST"),
        structured_output=structured,
    )
