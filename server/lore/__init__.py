from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

DEFAULT_LORE_PATH = Path(__file__).parent / "isekai.json"


class Guardian(BaseModel):
    name: str
    title: str
    persona: str
    disposition: str = "neutral"


class Region(BaseModel):
    name: str
    description: str


class WhoWhatWhereWhenWhy(BaseModel):
    who: str
    what: str
    where: str
    when: str
    why: str


class WorldBible(BaseModel):
    """A campaign's static, always-true setting facts - distinct from
    WorldState (server/state.py), which tracks what's actually *happened*
    during play (location, objectives, flags). A WorldBible never changes
    turn to turn; feeding it into the DM's system prompt keeps the world's
    own rules and history from drifting or contradicting themselves across
    a long session, the same "durable fact, not per-turn improvisation"
    reasoning behind every deterministic mechanic elsewhere in this
    project (real ability scores, real AC, real XP)."""

    setting_name: str
    tagline: str
    cosmology: str
    guardian: Guardian
    regions: list[Region]
    central_tension: str
    who_what_where_when_why: WhoWhatWhereWhenWhy
    tone_guidance: str

    def system_prompt_block(self) -> str:
        """Rendered once and appended to a NarratorBackend's system prompt
        (see server/narrator.py/narrator_ollama.py) - present on every
        call regardless of the rolling history window's own size, so the
        world's facts can't scroll out of context and get reinvented
        inconsistently on a long session the way turn-to-turn narration
        detail eventually does."""
        regions_text = "\n".join(f"- {r.name}: {r.description}" for r in self.regions)
        return (
            f"\n\nSETTING: {self.setting_name} - {self.tagline}\n{self.cosmology}\n\n"
            f"THE GUARDIAN: {self.guardian.name}, {self.guardian.title}. {self.guardian.persona}\n\n"
            f"KNOWN REGIONS:\n{regions_text}\n\n"
            f"CENTRAL TENSION: {self.central_tension}\n\n"
            f"TONE: {self.tone_guidance}\n\n"
            "These are fixed, durable facts about the world - stay consistent with them always, "
            "but you are not limited to only what's listed here; invent freely within this frame."
        )

    def opening_scene_prompt(self, present_description: str, plural: bool) -> str:
        """A deterministically-composed action_text for a genuinely fresh
        campaign's very first turn (server/engine.py's _on_start_session/
        _narrate_opening_scene) - the near-death/transport/Guardian-
        greeting beat. The actual who/what/where/when/why facts are
        supplied here, not left for the model to invent (and potentially
        contradict) on its own - the same "engine computes it, DM narrates
        around it" split every other mechanic in this project already
        uses, applied to the campaign's opening premise instead of a
        combat number."""
        w = self.who_what_where_when_why
        pronoun_clause = "each nearly died, in their own separate life, at nearly the same moment" if plural else "nearly died"
        intro = (
            f"(The adventure begins. Moments ago, {present_description} {pronoun_clause} - "
            f"now they awaken in {self.setting_name}, greeted by {self.guardian.name}, "
            f"{self.guardian.title}. Paraphrase these facts into {self.guardian.name}'s own "
            f"words and voice, dramatized in scene - don't quote them verbatim or recite them "
            f"as a list: {w.who} {w.what} {w.where} {w.when} {w.why}"
        )
        if plural:
            intro += " Consider inviting everyone to introduce themselves."
        return intro + " End the scene inviting the next real action.)"


def load_default_world_bible() -> WorldBible:
    return WorldBible.model_validate_json(DEFAULT_LORE_PATH.read_text())
