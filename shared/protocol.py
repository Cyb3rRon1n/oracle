from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    # client -> server
    "login",
    "tavern_chat",
    "join_session",
    "player_action",
    "chat_message",
    "character_edit",
    "dice_roll",
    "death_save",
    "leave_session",
    "reconnect",
    "start_session",
    "start_combat",
    "end_combat",
    # server -> client
    "login_result",
    "tavern_message",
    "tavern_directory",
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
    "left_session",
    "session_started",
    "system_message",
]


class Envelope(BaseModel):
    type: EventType
    # Required for every event type except login/login_result/
    # tavern_chat/tavern_message/tavern_directory, which happen before a
    # session is ever chosen - all send "" by convention (server/
    # accounts.py, client/app.py's login()) rather than making this field
    # Optional everywhere else, which would touch every other envelope-
    # constructing call site in the codebase for a distinction only
    # these types actually have.
    session_id: str
    sender_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "Envelope":
        return cls.model_validate_json(data)
