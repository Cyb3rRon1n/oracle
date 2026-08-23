"""OpenAI-compatible NarratorBackend - any endpoint speaking the
/chat/completions dialect (OpenAI, Deepseek, Kimi, Grok, ...). DM_BACKEND=openai,
configured via OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL.

Turn shape (docs/REBUILD_PLAN.md two-phase turn): a schema-constrained decide
call produces every structured decision (roll request, sheet deltas, world
deltas, scene facts) and never prose; the engine executes real rolls/tools;
a final unconstrained streaming call writes the actual narration. Constraining
JSON is cheap reliability; constraining prose flattens it - so prose is never
inside the schema. Reuses narrator_ollama's schemas/prompts wholesale."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable

import httpx

from .lore import WorldBible, load_default_world_bible
from .narrator import ApplyUpdate, RequestRoll, UpdateWorld, _turns_to_text
from .narrator_ollama import (
    DECIDE_FOLLOWUP_SCHEMA as _DECIDE_FOLLOWUP_SCHEMA,
    DECIDE_SCHEMA as _DECIDE_SCHEMA,
    STRUCTURED_OUTPUT_SYSTEM_PROMPT,
    WORLD_UPDATE_PROMPT_ADDENDUM,
    _outcome_update,
    _strip_narration,
    _with_scene_fields,
    _with_world_fields,
    _with_world_prompt,
)
from .rules import RulesIndex

logger = logging.getLogger(__name__)

SceneSink = Callable[[dict], None]

class OpenAINarrator:
    supports_scene_facts = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        rules: RulesIndex | None = None,
        world_bible: WorldBible | None = None,
        world_updates: bool = True,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self._model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._rules = rules or RulesIndex.load_default()
        lore_block = (world_bible or load_default_world_bible()).system_prompt_block()
        self._system_prompt = (
            _with_world_prompt(STRUCTURED_OUTPUT_SYSTEM_PROMPT, world_updates).replace(
                "Respond with a single JSON object matching the given schema",
                "Decide the outcome",
            )
            + lore_block
            + (WORLD_UPDATE_PROMPT_ADDENDUM if world_updates else "")
        )
        self._world_updates = world_updates
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=120,
        )

    async def _chat(self, messages: list[dict], schema: dict | None = None) -> dict:
        body: dict = {"model": self._model, "messages": messages}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "turn_decision", "schema": schema, "strict": False},
            }
        response = await self._client.post("/chat/completions", json=body)
        response.raise_for_status()
        return response.json()

    async def _chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        body = {"model": self._model, "messages": messages, "stream": True}
        async with self._client.stream("POST", "/chat/completions", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta

    def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
        world_summary: str | None = None,
        scene_sink: SceneSink | None = None,
    ) -> AsyncIterator[str]:
        return self._narrate(
            history, character_summary, action_text, apply_update, request_roll, update_world, world_summary, scene_sink
        )

    async def _decide(self, messages: list[dict], schema: dict) -> tuple[dict, list[dict]]:
        """One schema-constrained decision call, retried once if the endpoint
        still manages to return unparseable content - the validate-retry half
        of docs/REBUILD_PLAN.md's constrained-decoding item."""
        convo = [*messages]
        for attempt in range(2):
            response = await self._chat(convo, schema=schema)
            try:
                content = response["choices"][0]["message"]["content"] or ""
                return json.loads(content), messages
            except (json.JSONDecodeError, KeyError, IndexError):
                logger.warning("OpenAI decide call %s returned unparseable content; retrying", attempt + 1)
                convo = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "That was not valid JSON for the schema. Decide again."},
                ]
        return {}, messages

    @staticmethod
    def _prompt(character_summary: str, world_summary: str | None, action_text: str) -> str:
        prompt = f"Character:\n{character_summary}\n\n"
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        return prompt + f"Player action: {action_text}"

    async def _narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None,
        update_world: UpdateWorld | None,
        world_summary: str | None,
        scene_sink: SceneSink | None,
    ) -> AsyncIterator[str]:
        base_messages = [
            {"role": "system", "content": self._system_prompt},
            *history,
            {"role": "user", "content": self._prompt(character_summary, world_summary, action_text)},
        ]

        # Phase 1 - decide. Schema-constrained, no prose anywhere in scope.
        data, _ = await self._decide(base_messages, _with_scene_fields(_with_world_fields(_DECIDE_SCHEMA, self._world_updates)))

        roll_update: dict = {}
        if request_roll is not None and data.get("roll_requested"):
            if data.get("roll_skill"):
                roll_update["skill"] = data["roll_skill"]
            if data.get("roll_ability"):
                roll_update["ability"] = data["roll_ability"]
            if data.get("roll_dc") is not None:
                roll_update["dc"] = data["roll_dc"]
            if data.get("roll_kind"):
                roll_update["roll_kind"] = data["roll_kind"]
            roll_result_text = request_roll(roll_update)
            # Phase 1b - re-decide knowing the real roll result, so every
            # mechanical delta reflects what actually happened.
            data, _ = await self._decide(
                [
                    *base_messages,
                    {
                        "role": "user",
                        "content": f"Real dice result: {roll_result_text}\nDecide the outcome now.",
                    },
                ],
                _with_scene_fields(_with_world_fields(_DECIDE_FOLLOWUP_SCHEMA, self._world_updates)),
            )

        # Structured changes land BEFORE narration streams - the client sees
        # the sheet/world updates resolve as the prose describing them starts.
        if data.get("mechanical_change") or any(data.get(f) for f in ("rest", "notes", "disposition", "cast_spell")):
            apply_update(_outcome_update(data))
        if self._world_updates and data.get("world_change") and update_world is not None:
            world_delta: dict = {}
            for field in ("location", "mood", "add_objective", "complete_objective", "add_location"):
                if data.get(field):
                    world_delta[field] = data[field]
            if world_delta:
                update_world(world_delta)
        if scene_sink is not None:
            facts = {key: data.get(key) or [] for key in ("npcs_present", "points_of_interest", "suggested_actions")}
            facts["suggested_actions"] = facts["suggested_actions"][:4]
            scene_sink(facts)

        # Phase 2 - narrate. Unconstrained streaming prose, told what was
        # decided so it can't contradict the record.
        decided = {k: v for k, v in data.items() if k not in ("npcs_present", "points_of_interest", "suggested_actions")}
        narrate_messages = [
            {"role": "system", "content": "You are the Dungeon Master. Narrate the outcome in vivid, concise open-ended prose (3-5 sentences). Never break character. Output prose only."},
            *history,
            {
                "role": "user",
                "content": (
                    f"{self._prompt(character_summary, world_summary, action_text)}\n\n"
                    f"Decided outcome (narrate exactly this): {json.dumps(decided, ensure_ascii=False)}"
                ),
            },
        ]
        async for chunk in self._chat_stream(narrate_messages):
            yield chunk

    async def summarize(self, prior_summary: str, turns: list[dict]) -> str:
        prior = f"Summary so far:\n{prior_summary}\n\n" if prior_summary else ""
        response = await self._chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You maintain a running campaign summary for a tabletop RPG "
                        "session. Compress the recent events into a durable recap: "
                        "who the characters and recurring NPCs are, what happened, "
                        "what was promised or owed, and every unresolved thread. Keep "
                        "proper names exactly. At most ~180 words. Output only the "
                        "summary text."
                    ),
                },
                {"role": "user", "content": f"{prior}Recent turns:\n{_turns_to_text(turns)}"},
            ]
        )
        return (response["choices"][0]["message"]["content"] or "").strip()


def create_openai_narrator() -> OpenAINarrator:
    return OpenAINarrator()
