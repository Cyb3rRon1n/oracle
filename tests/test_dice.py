from __future__ import annotations

import pytest

from server import dice


def test_roll_single_die_within_bounds():
    total, rolls, sides = dice.roll("1d20")
    assert len(rolls) == 1
    assert 1 <= rolls[0] <= 20
    assert total == rolls[0]
    assert sides == 20


def test_roll_multiple_dice_with_positive_modifier():
    total, rolls, sides = dice.roll("2d6+3")
    assert len(rolls) == 2
    assert all(1 <= r <= 6 for r in rolls)
    assert total == sum(rolls) + 3
    assert sides == 6


def test_roll_with_negative_modifier():
    total, rolls, sides = dice.roll("1d4-1")
    assert total == rolls[0] - 1
    assert sides == 4


def test_roll_defaults_count_to_one():
    total, rolls, sides = dice.roll("d20")
    assert len(rolls) == 1
    assert sides == 20


@pytest.mark.parametrize("bad", ["", "xyz", "1d", "d", "0d6", "1d1", "101d6"])
def test_roll_rejects_invalid_notation(bad):
    with pytest.raises(dice.InvalidDiceNotation):
        dice.roll(bad)


def test_roll_extra_modifier_adds_on_top_of_the_notations_own_modifier():
    # server/engine.py's request_roll closure uses this for a caller-
    # supplied ability-score modifier - _DICE_RE's regex only supports one
    # signed modifier group in the notation string itself ("1d20+3", never
    # "1d20+3+2"), so a second modifier has to be summed in separately.
    total, rolls, sides = dice.roll("1d4+1", extra_modifier=2)
    assert total == rolls[0] + 1 + 2
    assert sides == 4


def test_roll_extra_modifier_defaults_to_zero():
    total, rolls, sides = dice.roll("1d20")
    assert total == rolls[0]
