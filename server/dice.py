from __future__ import annotations

import random
import re

_DICE_RE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$", re.IGNORECASE)


class InvalidDiceNotation(ValueError):
    pass


def roll(notation: str) -> tuple[int, list[int]]:
    """Roll dice notation like '1d20' or '2d6+3'. Returns (total, individual_rolls)."""
    match = _DICE_RE.match(notation.strip())
    if not match:
        raise InvalidDiceNotation(
            f"'{notation}' isn't valid dice notation (expected e.g. '1d20', '2d6+3')"
        )

    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = int(match.group(3) or 0)

    if not 1 <= count <= 100:
        raise InvalidDiceNotation("dice count must be between 1 and 100")
    if sides < 2:
        raise InvalidDiceNotation("dice must have at least 2 sides")

    rolls = [random.randint(1, sides) for _ in range(count)]
    return sum(rolls) + modifier, rolls
