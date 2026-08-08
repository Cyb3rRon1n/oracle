from __future__ import annotations

import random
import re

_DICE_RE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$", re.IGNORECASE)


class InvalidDiceNotation(ValueError):
    pass


def roll(
    notation: str, extra_modifier: int = 0, advantage: bool = False, disadvantage: bool = False
) -> tuple[int, list[int], int]:
    """Roll dice notation like '1d20' or '2d6+3'. Returns
    (total, individual_rolls, sides) - sides is returned (not just parsed
    and discarded, as before) so a caller can tell a natural max/min roll
    apart from an ordinary one without re-parsing the notation string
    itself: needed by the dice_result envelope, which reports individual
    rolls but shouldn't make every consumer duplicate this module's own
    notation parser just to know what "natural 20" means for a given die.

    extra_modifier is added to the total on top of whatever modifier the
    notation string itself carries - server/engine.py's request_roll
    closure uses this for a caller-supplied ability-score modifier
    (CharacterSheet.stat_modifiers) rather than asking a caller to splice
    a second signed number into the notation string itself, which
    _DICE_RE's single-modifier-group regex doesn't support anyway (no
    "1d20+3+2"). Kept separate from `modifier` in the return-value math
    only conceptually - both are just added to the same total; callers
    that care about the distinction (e.g. for display) track it
    themselves, this function only needs the combined number.

    advantage/disadvantage are real 5e's own "roll twice, keep the
    better/worse" mechanic - both `rolls` entries are returned either way
    so a caller can show the discarded roll too, not just the kept one.
    Deliberately only applies to a genuine single d20 (count=1, sides=20)
    - real 5e never applies advantage/disadvantage to damage rolls or
    anything else, so a caller passing it for other notation is a silent
    no-op rather than an error, the same "not applicable" convention this
    module already follows elsewhere. Both flags true cancel out to
    neither, real 5e's own rule for when both apply at once - a caller
    doesn't need to resolve that itself."""
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

    if advantage and disadvantage:
        advantage = disadvantage = False

    if (advantage or disadvantage) and count == 1 and sides == 20:
        rolls = [random.randint(1, 20), random.randint(1, 20)]
        kept = max(rolls) if advantage else min(rolls)
        return kept + modifier + extra_modifier, rolls, sides

    rolls = [random.randint(1, sides) for _ in range(count)]
    return sum(rolls) + modifier + extra_modifier, rolls, sides
