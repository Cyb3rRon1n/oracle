from __future__ import annotations

from server.portrait import build_portrait_prompt
from server.state import CharacterSheet


def _character(**overrides) -> CharacterSheet:
    defaults = dict(player_id="p1", name="Thrain Ironveil", hp=20, max_hp=20)
    defaults.update(overrides)
    return CharacterSheet(**defaults)


def test_build_portrait_prompt_includes_name_race_and_class():
    character = _character(race="dwarf", character_class="Fighter")
    prompt = build_portrait_prompt(character)
    assert "Thrain Ironveil" in prompt
    assert "a dwarf Fighter" in prompt


def test_build_portrait_prompt_uses_an_for_a_vowel_leading_race():
    character = _character(race="elf", character_class="Wizard")
    prompt = build_portrait_prompt(character)
    assert "an elf Wizard" in prompt


def test_build_portrait_prompt_includes_class_visual_tag():
    character = _character(race="human", character_class="Rogue")
    prompt = build_portrait_prompt(character)
    assert "dark leathers" in prompt


def test_build_portrait_prompt_includes_race_visual_tag_for_a_subrace():
    character = _character(race="high_elf", character_class="Cleric")
    prompt = build_portrait_prompt(character)
    assert "aristocratic bearing" in prompt


def test_build_portrait_prompt_has_no_race_tag_for_human():
    # No _RACE_VISUALS entry for human - real 5e's own framing is that
    # humans have no single defining trait to draw a tag from.
    character = _character(race="human", character_class="Fighter")
    prompt = build_portrait_prompt(character)
    assert "battle-worn plate" in prompt  # class tag still present
    assert "pointed ears" not in prompt  # no elf/dwarf/halfling tag leaked in


def test_build_portrait_prompt_includes_background():
    character = _character(race="halfling", character_class="Rogue", background="a reformed cutpurse")
    prompt = build_portrait_prompt(character)
    assert "a reformed cutpurse" in prompt


def test_build_portrait_prompt_falls_back_gracefully_for_blank_race_and_class():
    character = _character(race="", character_class="")
    prompt = build_portrait_prompt(character)
    assert "a human adventurer" in prompt


def test_build_portrait_prompt_always_has_style_suffix():
    character = _character(race="human", character_class="")
    prompt = build_portrait_prompt(character)
    assert "digital painting" in prompt
