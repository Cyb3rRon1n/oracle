from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    # client -> server
    "join_session",
    "player_action",
    "chat_message",
    "character_edit",
    "dice_roll",
    "reconnect",
    # server -> client
    "state_sync",
    "log_entry",
    "character_update",
    "player_update",
    "npc_update",
    "world_update",
    "turn_prompt",
    "dice_result",
    "player_joined",
    "player_left",
    "system_message",
]


class Envelope(BaseModel):
    type: EventType
    session_id: str
    sender_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "Envelope":
        return cls.model_validate_json(data)
