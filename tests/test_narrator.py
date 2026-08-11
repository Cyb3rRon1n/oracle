from __future__ import annotations

from server.narrator import AnthropicNarrator
from server.rules import RulesIndex


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class FakeMessage:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class FakeStream:
    def __init__(self, chunks, final_message):
        self._final_message = final_message
        self.text_stream = self._make_gen(chunks)

    async def _make_gen(self, chunks):
        for chunk in chunks:
            yield chunk

    async def get_final_message(self):
        return self._final_message


class FakeStreamContext:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *args):
        return False


class FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        chunks, final_message = self._responses.pop(0)
        return FakeStreamContext(FakeStream(chunks, final_message))


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessagesAPI(responses)


def make_narrator() -> AnthropicNarrator:
    return AnthropicNarrator(api_key="test-key", rules=RulesIndex.load_default())


def noop_apply_update(update: dict) -> str:
    return "unexpected call"


def test_rules_index_lookup_known_and_unknown():
    idx = RulesIndex.load_default()

    goblin = idx.lookup("monster", "Goblin")
    assert "Goblin" in goblin
    assert '"ac": 15' in goblin

    assert idx.lookup("monster", "beholder").startswith("No local SRD entry")
    assert idx.lookup("bogus-category", "x").startswith("Unknown category")


async def test_narrate_streams_text_when_no_tool_use():
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["You ", "enter ", "the tavern."], final)])

    chunks = [c async for c in narrator.narrate([], "{}", "I enter the tavern", noop_apply_update)]

    assert "".join(chunks) == "You enter the tavern."
    assert len(narrator._client.messages.calls) == 1


async def test_narrate_prepends_rolling_history_to_the_request():
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["Okay."], final)])
    history = [
        {"role": "user", "content": "I attack the goblin"},
        {"role": "assistant", "content": "You swing your sword."},
    ]

    [c async for c in narrator.narrate(history, "{}", "I check my inventory", noop_apply_update)]

    sent_messages = narrator._client.messages.calls[0]["messages"]
    assert sent_messages[0] == history[0]
    assert sent_messages[1] == history[1]
    assert "I check my inventory" in sent_messages[2]["content"]


async def test_narrate_includes_world_summary_when_given():
    # Given directly rather than left for the model to infer from history
    # alone - see NarratorBackend.narrate's own docstring (server/
    # narrator.py) and ROADMAP.md's update_world reliability investigation.
    # Anthropic never needed this to run at all (no world_updates-style
    # opt-in gate) - gets the same context unconditionally.
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["Okay."], final)])

    [
        c async for c in narrator.narrate(
            [], "{}", "I look around", noop_apply_update,
            world_summary="Current location: Millbrook\nActive objectives:\n- Find the missing goat",
        )
    ]

    sent_content = narrator._client.messages.calls[0]["messages"][0]["content"]
    assert "World state:" in sent_content
    assert "Find the missing goat" in sent_content


async def test_narrate_omits_world_summary_section_when_not_given():
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["Okay."], final)])

    [c async for c in narrator.narrate([], "{}", "I look around", noop_apply_update)]

    sent_content = narrator._client.messages.calls[0]["messages"][0]["content"]
    assert "World state:" not in sent_content


async def test_narrate_executes_lookup_rule_tool_and_continues():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_1", name="lookup_rule", input={"category": "monster", "name": "goblin"}
    )
    first = FakeMessage(stop_reason="tool_use", content=[tool_call])
    second = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient(
        [
            (["A goblin leaps out"], first),
            ([" and attacks!"], second),
        ]
    )

    chunks = [c async for c in narrator.narrate([], "{}", "I open the door", noop_apply_update)]

    assert "".join(chunks) == "A goblin leaps out and attacks!"
    assert len(narrator._client.messages.calls) == 2

    second_call_messages = narrator._client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert "Goblin" in tool_result_content
    assert '"ac": 15' in tool_result_content


async def test_narrate_routes_update_character_tool_to_callback():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_2", name="update_character", input={"hp_delta": -4}
    )
    first = FakeMessage(stop_reason="tool_use", content=[tool_call])
    second = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient(
        [
            (["You take a hit."], first),
            ([" You reel."], second),
        ]
    )

    received_updates = []

    def apply_update(update: dict) -> str:
        received_updates.append(update)
        return "Applied: HP -4 (now 6/10)."

    chunks = [
        c async for c in narrator.narrate([], "{}", "I stand my ground", apply_update)
    ]

    assert "".join(chunks) == "You take a hit. You reel."
    assert received_updates == [{"hp_delta": -4}]

    second_call_messages = narrator._client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert tool_result_content == "Applied: HP -4 (now 6/10)."


async def test_narrate_routes_request_roll_tool_to_callback():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_3", name="request_roll", input={"dice": "1d20+2", "dc": 12, "reason": "attack"}
    )
    first = FakeMessage(stop_reason="tool_use", content=[tool_call])
    second = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient(
        [
            (["You swing."], first),
            ([" It connects!"], second),
        ]
    )

    received_rolls = []

    def request_roll(update: dict) -> str:
        received_rolls.append(update)
        return "Rolled 1d20+2: 15 [13] vs DC 12 — success."

    chunks = [
        c async for c in narrator.narrate([], "{}", "I attack", noop_apply_update, request_roll)
    ]

    assert "".join(chunks) == "You swing. It connects!"
    assert received_rolls == [{"dice": "1d20+2", "dc": 12, "reason": "attack"}]

    second_call_messages = narrator._client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert tool_result_content == "Rolled 1d20+2: 15 [13] vs DC 12 — success."


async def test_narrate_without_request_roll_callback_still_completes():
    # Existing callers (including every other test in this file) don't pass
    # request_roll - the default must not break narration for models/tests
    # that never call the tool.
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["Fine."], final)])

    chunks = [c async for c in narrator.narrate([], "{}", "I wait", noop_apply_update)]

    assert "".join(chunks) == "Fine."


async def test_narrate_routes_update_world_tool_to_callback():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_4", name="update_world", input={"add_objective": "Find the missing merchant"}
    )
    first = FakeMessage(stop_reason="tool_use", content=[tool_call])
    second = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient(
        [
            (["You press on."], first),
            ([" A new lead emerges."], second),
        ]
    )

    received_updates = []

    def update_world(update: dict) -> str:
        received_updates.append(update)
        return "Applied: new objective: 'Find the missing merchant'."

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I ask around town", noop_apply_update, None, update_world
        )
    ]

    assert "".join(chunks) == "You press on. A new lead emerges."
    assert received_updates == [{"add_objective": "Find the missing merchant"}]

    second_call_messages = narrator._client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert tool_result_content == "Applied: new objective: 'Find the missing merchant'."


async def test_narrate_without_update_world_callback_still_completes():
    narrator = make_narrator()
    final = FakeMessage(stop_reason="end_turn", content=[])
    narrator._client = FakeClient([(["Fine."], final)])

    chunks = [c async for c in narrator.narrate([], "{}", "I wait", noop_apply_update)]

    assert "".join(chunks) == "Fine."


async def test_narrate_stops_after_max_tool_rounds_without_hanging():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_x", name="lookup_rule", input={"category": "monster", "name": "goblin"}
    )
    always_calls_tool = FakeMessage(stop_reason="tool_use", content=[tool_call])
    narrator._client = FakeClient([([], always_calls_tool) for _ in range(4)])

    chunks = [c async for c in narrator.narrate([], "{}", "I keep fighting", noop_apply_update)]

    assert chunks == []
    assert len(narrator._client.messages.calls) == 4
