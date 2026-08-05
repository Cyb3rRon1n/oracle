from __future__ import annotations

from pydantic import BaseModel, Field


class CharacterSheet(BaseModel):
    player_id: str
    name: str
    hp: int
    max_hp: int
    stats: dict[str, int] = Field(default_factory=dict)
    inventory: list[str] = Field(default_factory=list)
    notes: str = ""


class WorldState(BaseModel):
    location: str = "unknown"
    summary: str = ""
    flags: dict[str, bool] = Field(default_factory=dict)


class Session(BaseModel):
    session_id: str
    characters: dict[str, CharacterSheet] = Field(default_factory=dict)
    world: WorldState = Field(default_factory=WorldState)
    turn_order: list[str] = Field(default_factory=list)
    current_turn_index: int = 0
    log: list[dict] = Field(default_factory=list)

    @property
    def current_turn(self) -> str | None:
        if not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index % len(self.turn_order)]

    def advance_turn(self) -> None:
        if self.turn_order:
            self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)
