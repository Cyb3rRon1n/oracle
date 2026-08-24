from __future__ import annotations

from unittest.mock import patch

from server.lore import (
    Faction,
    GlossaryEntry,
    Guardian,
    HistoryEvent,
    Origin,
    OriginTable,
    Region,
    WhoWhatWhereWhenWhy,
    WorldBible,
    load_default_origin_table,
    load_default_world_bible,
    random_origin,
)


def make_world_bible(**overrides) -> WorldBible:
    defaults = dict(
        setting_name="Testonia",
        tagline="A place for testing.",
        cosmology="It exists solely to be asserted against.",
        guardian=Guardian(name="Testwarden", title="the Fixture", persona="Reliable."),
        regions=[
            Region(name="The Only Region", description="There is only one."),
            Region(name="A Second Region", description="Exists for the multi-region test below."),
        ],
        central_tension="Will the assertions pass?",
        who_what_where_when_why=WhoWhatWhereWhenWhy(
            who="A test subject.", what="A test event.", where="A test place.",
            when="Test time.", why="For coverage.",
        ),
        tone_guidance="Dry and deterministic.",
    )
    defaults.update(overrides)
    return WorldBible(**defaults)


def test_load_default_world_bible_parses_the_bundled_file():
    bible = load_default_world_bible()
    assert bible.setting_name == "Aetherfall"
    assert bible.guardian.name == "Ashwren"
    assert len(bible.regions) >= 1


def test_default_world_bible_carries_depth_sections():
    bible = load_default_world_bible()
    assert len(bible.history) >= 3, "a layered past is the point"
    assert len(bible.factions) >= 3
    assert len(bible.peoples) >= 3
    assert len(bible.glossary) >= 3
    assert bible.geography_notes


def test_system_prompt_block_renders_depth_sections_when_populated():
    bible = make_world_bible(
        history=[HistoryEvent(era="old", name="The Founding", description="Testonia was founded.")],
        factions=[Faction(name="The Testers", kind="guild", description="They test things.")],
        peoples=[Region(name="The Testfolk", description="They live here.")],
        glossary=[GlossaryEntry(term="Plain compounds", meaning="name old places plainly.")],
        geography_notes="Every road leads to the test.",
    )
    block = bible.system_prompt_block()

    assert "HISTORY (oldest first):" in block
    assert "[old] The Founding: Testonia was founded." in block
    assert "FACTIONS:" in block
    assert "- The Testers (guild): They test things." in block
    assert "PEOPLES:" in block
    assert "NAMING & PLACES" in block
    assert "GEOGRAPHY: Every road leads to the test." in block


def test_system_prompt_block_omits_empty_depth_sections():
    block = make_world_bible().system_prompt_block()

    assert "HISTORY" not in block
    assert "FACTIONS" not in block
    assert "PEOPLES" not in block
    assert "NAMING" not in block
    assert "GEOGRAPHY" not in block


def test_region_borders_and_landmarks_render_in_the_regions_list():
    bible = make_world_bible()
    bible.regions[0].borders = ["the edge of the map"]
    bible.regions[0].landmarks = ["the fixture stone"]

    block = bible.system_prompt_block()

    assert "Borders: the edge of the map." in block
    assert "Landmarks: the fixture stone." in block


def test_system_prompt_block_includes_every_field():
    bible = make_world_bible()
    block = bible.system_prompt_block()

    assert "Testonia" in block
    assert "A place for testing." in block
    assert "It exists solely to be asserted against." in block
    assert "Testwarden" in block
    assert "the Fixture" in block
    assert "Reliable." in block
    assert "The Only Region" in block
    assert "A Second Region" in block
    assert "Will the assertions pass?" in block
    assert "Dry and deterministic." in block


def test_opening_scene_prompt_singular_names_the_character_and_guardian():
    bible = make_world_bible()
    prompt = bible.opening_scene_prompt("Thrain", plural=False)

    assert "Thrain" in prompt
    assert "nearly died" in prompt
    assert "Testonia" in prompt
    assert "Testwarden" in prompt
    assert "A test subject." in prompt
    assert "introduce themselves" not in prompt  # a solo-player nudge, not appropriate here


def test_opening_scene_prompt_plural_invites_introductions():
    bible = make_world_bible()
    prompt = bible.opening_scene_prompt("Thrain the Fighter, Rowan the Rogue", plural=True)

    assert "Thrain the Fighter, Rowan the Rogue" in prompt
    assert "each nearly died" in prompt
    assert "introduce themselves" in prompt


def test_opening_scene_prompt_always_mentions_adventure_beginning():
    # tests/test_engine.py's own opening-scene tests assert this substring
    # on the composed action_text - locking it in here at the source.
    bible = make_world_bible()
    assert "adventure begins" in bible.opening_scene_prompt("Thrain", plural=False)


def test_opening_scene_prompt_includes_origin_detail_when_given():
    bible = make_world_bible()
    prompt = bible.opening_scene_prompt(
        "Thrain", plural=False, origin_detail="A lighthouse keeper. Nearly died: a storm."
    )
    assert "A lighthouse keeper. Nearly died: a storm." in prompt


def test_opening_scene_prompt_omits_origin_section_when_blank():
    bible = make_world_bible()
    prompt = bible.opening_scene_prompt("Thrain", plural=False, origin_detail="")
    assert "specific background" not in prompt


def test_load_default_origin_table_parses_the_bundled_file():
    table = load_default_origin_table()
    assert len(table.backgrounds) >= 1
    assert len(table.traits) >= 1
    assert len(table.near_death_events) >= 1


def test_random_origin_picks_from_the_given_table():
    table = OriginTable(backgrounds=["a baker"], traits=["stubborn"], near_death_events=["a fall"])
    origin = random_origin(table)
    assert origin == Origin(background="a baker", trait="stubborn", near_death="a fall")


def test_random_origin_is_mockable_the_same_way_dice_rolls_are():
    table = OriginTable(
        backgrounds=["a baker", "a nurse"], traits=["stubborn", "curious"], near_death_events=["a fall", "a fire"]
    )
    with patch("server.lore.random.choice", side_effect=["a nurse", "curious", "a fire"]):
        origin = random_origin(table)
    assert origin == Origin(background="a nurse", trait="curious", near_death="a fire")


def test_origin_sheet_summary_reads_as_a_standalone_paragraph():
    origin = Origin(background="a dockworker", trait="stubborn to a fault", near_death="a fall from scaffolding")
    summary = origin.sheet_summary()
    assert summary == "A dockworker. Known for being stubborn to a fault. Nearly died: a fall from scaffolding."
