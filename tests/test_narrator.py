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

    chunks = [c async for c in narrator.narrate([], "{}", "I enter the tavern")]

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

    [c async for c in narrator.narrate(history, "{}", "I check my inventory")]

    sent_messages = narrator._client.messages.calls[0]["messages"]
    assert sent_messages[0] == history[0]
    assert sent_messages[1] == history[1]
    assert "I check my inventory" in sent_messages[2]["content"]


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

    chunks = [c async for c in narrator.narrate([], "{}", "I open the door")]

    assert "".join(chunks) == "A goblin leaps out and attacks!"
    assert len(narrator._client.messages.calls) == 2

    second_call_messages = narrator._client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert "Goblin" in tool_result_content
    assert '"ac": 15' in tool_result_content


async def test_narrate_stops_after_max_tool_rounds_without_hanging():
    narrator = make_narrator()
    tool_call = FakeToolUseBlock(
        id="tu_x", name="lookup_rule", input={"category": "monster", "name": "goblin"}
    )
    always_calls_tool = FakeMessage(stop_reason="tool_use", content=[tool_call])
    narrator._client = FakeClient([([], always_calls_tool) for _ in range(4)])

    chunks = [c async for c in narrator.narrate([], "{}", "I keep fighting")]

    assert chunks == []
    assert len(narrator._client.messages.calls) == 4
