from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Protocol

import anthropic

DM_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action. Never break character."""


class NarratorBackend(Protocol):
    def narrate(
        self, world_summary: str, character_summary: str, action_text: str
    ) -> AsyncIterator[str]:
        """Stream narration text in response to a player's action."""


class AnthropicNarrator:
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-5"):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def narrate(
        self, world_summary: str, character_summary: str, action_text: str
    ) -> AsyncIterator[str]:
        prompt = (
            f"World state:\n{world_summary or '(session just started)'}\n\n"
            f"Character:\n{character_summary}\n\n"
            f"Player action: {action_text}"
        )
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=1024,
            system=DM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                yield chunk


def create_narrator(backend: str | None = None) -> NarratorBackend:
    """Backend selector. Add new NarratorBackend implementations here (e.g. an
    Ollama-backed local narrator) and register them by name to keep the
    engine and transport layers unaware of which one is in use."""
    backend = backend or os.environ.get("DM_BACKEND", "anthropic")
    if backend == "anthropic":
        return AnthropicNarrator(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    raise ValueError(f"Unknown DM_BACKEND {backend!r}. Only 'anthropic' is implemented so far.")
