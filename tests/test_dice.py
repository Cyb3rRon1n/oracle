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
