from __future__ import annotations

from server.lore import Guardian, Region, WhoWhatWhereWhenWhy, WorldBible, load_default_world_bible


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
