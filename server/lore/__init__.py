from __future__ import annotations

import random
from pathlib import Path

from pydantic import BaseModel

DEFAULT_LORE_PATH = Path(__file__).parent / "isekai.json"
DEFAULT_ORIGIN_TABLE_PATH = Path(__file__).parent / "origins.json"


class Guardian(BaseModel):
    name: str
    title: str
    persona: str
    disposition: str = "neutral"


class Region(BaseModel):
    name: str
    description: str
    borders: list[str] = []
    landmarks: list[str] = []
    # Optional canvas coordinates (0-1000) - present on the default bible's
    # regions so the engine can seed the campaign map with the known world
    # at session start; absent (None) means "no placement", and a custom
    # bible without coords simply seeds nothing.
    x: int | None = None
    y: int | None = None


class WhoWhatWhereWhenWhy(BaseModel):
    who: str
    what: str
    where: str
    when: str
    why: str


class HistoryEvent(BaseModel):
    """One entry in the world's timeline, oldest first - the layered-past
    structure that makes a setting feel deep rather than assembled: places
    and factions can reference eras, and the DM can date anything a player
    asks about without improvising contradictions."""

    era: str
    name: str
    description: str


class Faction(BaseModel):
    name: str
    kind: str
    description: str


class GlossaryEntry(BaseModel):
    """Naming-convention rules rather than a dictionary - the structural
    trick behind worlds whose invented names feel coherent instead of
    random: consistent patterns per culture/place-class, so anything the
    DM makes up on the fly sounds like it belongs to the same world."""

    term: str
    meaning: str


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
    history: list[HistoryEvent] = []
    factions: list[Faction] = []
    peoples: list[Region] = []
    glossary: list[GlossaryEntry] = []
    geography_notes: str = ""

    def system_prompt_block(self) -> str:
        """Rendered once and appended to a NarratorBackend's system prompt
        (see server/narrator.py/narrator_ollama.py) - present on every
        call regardless of the rolling history window's own size, so the
        world's facts can't scroll out of context and get reinvented
        inconsistently on a long session the way turn-to-turn narration
        detail eventually does. The depth sections (history/factions/
        peoples/glossary/geography) render only when populated, so a
        minimal custom bible costs no extra prompt tokens."""
        regions_text = "\n".join(
            f"- {r.name}: {r.description}"
            + (f" Borders: {'; '.join(r.borders)}." if r.borders else "")
            + (f" Landmarks: {'; '.join(r.landmarks)}." if r.landmarks else "")
            for r in self.regions
        )
        block = (
            f"\n\nSETTING: {self.setting_name} - {self.tagline}\n{self.cosmology}\n\n"
            f"THE GUARDIAN: {self.guardian.name}, {self.guardian.title}. {self.guardian.persona}\n\n"
            f"KNOWN REGIONS:\n{regions_text}\n\n"
        )
        if self.history:
            events = "\n".join(f"- [{h.era}] {h.name}: {h.description}" for h in self.history)
            block += f"HISTORY (oldest first):\n{events}\n\n"
        if self.factions:
            facs = "\n".join(f"- {f.name} ({f.kind}): {f.description}" for f in self.factions)
            block += f"FACTIONS:\n{facs}\n\n"
        if self.peoples:
            peps = "\n".join(f"- {p.name}: {p.description}" for p in self.peoples)
            block += f"PEOPLES:\n{peps}\n\n"
        if self.glossary:
            terms = "\n".join(f"- {g.term}: {g.meaning}" for g in self.glossary)
            block += f"NAMING & PLACES - invent new names within these patterns:\n{terms}\n\n"
        if self.geography_notes:
            block += f"GEOGRAPHY: {self.geography_notes}\n\n"
        block += (
            f"CENTRAL TENSION: {self.central_tension}\n\n"
            f"TONE: {self.tone_guidance}\n\n"
            "These are fixed, durable facts about the world - stay consistent with them always, "
            "but you are not limited to only what's listed here; invent freely within this frame."
        )
        return block

    def opening_scene_prompt(self, present_description: str, plural: bool, origin_detail: str = "") -> str:
        """A deterministically-composed action_text for a genuinely fresh
        campaign's very first turn (server/engine.py's _on_start_session/
        _narrate_opening_scene) - the near-death/transport/Guardian-
        greeting beat. The actual who/what/where/when/why facts are
        supplied here, not left for the model to invent (and potentially
        contradict) on its own - the same "engine computes it, DM narrates
        around it" split every other mechanic in this project already
        uses, applied to the campaign's opening premise instead of a
        combat number.

        origin_detail (see Origin.sheet_summary below - the same text
        stored on the character sheet and shown there, reused here rather
        than maintained as a second phrasing) personalizes a solo
        player's specific near-death moment instead of the generic
        who_what_where_when_why phrasing alone - left blank for a
        multi-player start, since there's no single origin to anchor a
        shared greeting on without picking one character's over the
        others'."""
        w = self.who_what_where_when_why
        pronoun_clause = "each nearly died, in their own separate life, at nearly the same moment" if plural else "nearly died"
        intro = (
            f"(The adventure begins. Moments ago, {present_description} {pronoun_clause} - "
            f"now they awaken in {self.setting_name}, greeted by {self.guardian.name}, "
            f"{self.guardian.title}. Paraphrase these facts into {self.guardian.name}'s own "
            f"words and voice, dramatized in scene - don't quote them verbatim or recite them "
            f"as a list: {w.who} {w.what} {w.where} {w.when} {w.why}"
        )
        if origin_detail:
            intro += (
                f" {present_description}'s specific background, for {self.guardian.name} to sense "
                f"or reference naturally rather than recite outright: {origin_detail}"
            )
        if plural:
            intro += " Consider inviting everyone to introduce themselves."
        return intro + " End the scene inviting the next real action.)"


def load_default_world_bible() -> WorldBible:
    return WorldBible.model_validate_json(DEFAULT_LORE_PATH.read_text())


class Origin(BaseModel):
    """A randomly-generated pre-Aetherfall identity for one character -
    who they were, a defining trait, and how they nearly died. Generated
    once at character creation (server/engine.py's build_starting_character)
    and stored on the sheet, not regenerated each time it's referenced -
    a character's origin is a fixed fact about them, not something that
    should drift."""

    background: str
    trait: str
    near_death: str

    def sheet_summary(self) -> str:
        """Player/DM-facing text for the character sheet's Features &
        Notes tab (client/app.py's CharacterSheetPanel) - who this
        character was before Aetherfall, at a glance. Reused as-is for
        WorldBible.opening_scene_prompt's origin_detail rather than
        maintained as a second phrasing."""
        return f"{self.background.capitalize()}. Known for being {self.trait}. Nearly died: {self.near_death}."


class OriginTable(BaseModel):
    backgrounds: list[str]
    traits: list[str]
    near_death_events: list[str]


def load_default_origin_table() -> OriginTable:
    return OriginTable.model_validate_json(DEFAULT_ORIGIN_TABLE_PATH.read_text())


def random_origin(table: OriginTable) -> Origin:
    # random.choice, not the engine's own dice.roll() - this isn't a game
    # mechanic with a real probability distribution to get right, just an
    # even pick from a curated table, the same "plain random.choice" this
    # project's other flavor-text generation (none existed before this)
    # would use. Mockable the same way tests/test_engine.py already mocks
    # server.dice.random.randint for reproducible dice-roll tests.
    return Origin(
        background=random.choice(table.backgrounds),
        trait=random.choice(table.traits),
        near_death=random.choice(table.near_death_events),
    )
