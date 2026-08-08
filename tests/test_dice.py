from __future__ import annotations

from unittest.mock import patch

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


def test_roll_disadvantage_keeps_the_lower_of_two_d20s():
    with patch("server.dice.random.randint", side_effect=[14, 8]):
        total, rolls, sides = dice.roll("1d20", disadvantage=True)
    assert rolls == [14, 8]  # both real rolls reported, not just the kept one
    assert total == 8
    assert sides == 20


def test_roll_advantage_keeps_the_higher_of_two_d20s():
    with patch("server.dice.random.randint", side_effect=[8, 14]):
        total, rolls, sides = dice.roll("1d20", advantage=True)
    assert rolls == [8, 14]
    assert total == 14


def test_roll_advantage_and_disadvantage_together_cancel_out_to_a_normal_roll():
    # Real 5e's own rule: both at once means neither applies.
    with patch("server.dice.random.randint", return_value=11):
        total, rolls, sides = dice.roll("1d20", advantage=True, disadvantage=True)
    assert len(rolls) == 1
    assert total == 11


def test_roll_disadvantage_includes_the_notations_own_and_extra_modifier():
    with patch("server.dice.random.randint", side_effect=[14, 8]):
        total, rolls, sides = dice.roll("1d20+3", extra_modifier=2, disadvantage=True)
    assert total == 8 + 3 + 2  # the kept (lower) roll, plus both modifiers


def test_roll_disadvantage_is_a_no_op_for_anything_other_than_a_single_d20():
    # Real 5e never applies advantage/disadvantage to damage rolls or
    # multi-die/non-d20 notation - a caller passing it anyway is a silent
    # no-op, not an error, matching this module's existing conventions.
    total, rolls, sides = dice.roll("2d6", disadvantage=True)
    assert len(rolls) == 2
    assert sides == 6

    total, rolls, sides = dice.roll("1d8", disadvantage=True)
    assert len(rolls) == 1
    assert sides == 8
