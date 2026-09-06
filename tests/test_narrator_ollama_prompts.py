"""Golden snapshot of every system prompt OllamaNarrator assembles.

The Ollama narrator builds ~10 prompt variants - some literal, some derived by
string surgery, some composed at __init__ time from opt-in addenda. This locks
every one of them: a change to prompt text (deliberate or accidental) fails
here with a diff, so a refactor of the construction can prove byte-identity and
a wording tweak is a conscious, reviewed change.

Regenerate after an intended prompt change: `UPDATE_GOLDEN=1 pytest -q -k ollama_prompts`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from server import narrator_ollama as m
from server.narrator_ollama import OllamaNarrator

GOLDEN = Path(__file__).parent / "golden" / "ollama_prompts.json"


class _StubBible:
    """Stands in for a real WorldBible so the snapshot captures prompt-assembly
    logic, not the (separately-tested) bible content."""

    def system_prompt_block(self) -> str:
        return "\n\n[[BIBLE]]"


# Flag combos worth pinning - the default, and each opt-in on its own.
_CONFIGS = {
    "default": {},
    "roll_requests": {"roll_requests": True},
    "world_updates": {"world_updates": True},
    "fact_ledger": {"fact_ledger": True},
    "few_shot": {"few_shot_example": True},
    "hardened_rules": {"hardened_rules": True},
    "tool_calling": {"structured_output": False},
}

_PROMPT_ATTRS = (
    "_tool_calling_system_prompt",
    "_structured_system_prompt",
    "_structured_roll_system_prompt",
    "_structured_followup_system_prompt",
    "_decide_system_prompt",
    "_decide_followup_system_prompt",
)


def _snapshot() -> dict:
    snap: dict[str, dict[str, str]] = {"module_constants": {}, "assembled": {}}
    for name in dir(m):
        if name.endswith(("_SYSTEM_PROMPT", "_PROMPT_ADDENDUM", "_FEW_SHOT_EXAMPLE")):
            snap["module_constants"][name] = getattr(m, name)
    for cfg_name, kwargs in _CONFIGS.items():
        narrator = OllamaNarrator(world_bible=_StubBible(), **kwargs)
        snap["assembled"][cfg_name] = {
            attr: getattr(narrator, attr) for attr in _PROMPT_ATTRS
        }
    return snap


def test_ollama_prompts_match_golden():
    current = _snapshot()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
        return
    assert GOLDEN.exists(), "run `UPDATE_GOLDEN=1 pytest -k ollama_prompts` to create the golden file"
    expected = json.loads(GOLDEN.read_text())
    assert current == expected, (
        "Ollama prompt text changed. If intended, regenerate: "
        "UPDATE_GOLDEN=1 pytest -q -k ollama_prompts"
    )
