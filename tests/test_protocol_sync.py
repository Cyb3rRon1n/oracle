"""The Python and JS protocol definitions have no shared source, so they can
drift silently - and have (a new event type added to one, not the other). This
guard fails CI the moment the two event-type sets disagree."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _python_event_types() -> set[str]:
    text = (ROOT / "shared" / "protocol.py").read_text()
    block = re.search(r"EventType = Literal\[(.*?)\]", text, re.S)
    assert block, "couldn't find the EventType Literal in shared/protocol.py"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def _js_event_types() -> set[str]:
    text = (ROOT / "web" / "src" / "lib" / "protocol.js").read_text()
    block = re.search(r"export const EventType = \{(.*?)\};", text, re.S)
    assert block, "couldn't find `export const EventType` in web/src/lib/protocol.js"
    return set(re.findall(r':\s*"([a-z_]+)"', block.group(1)))


def test_python_and_js_event_types_match():
    py, js = _python_event_types(), _js_event_types()
    assert py == js, (
        f"protocol event types out of sync\n"
        f"  only in shared/protocol.py: {sorted(py - js)}\n"
        f"  only in web/src/lib/protocol.js: {sorted(js - py)}"
    )
