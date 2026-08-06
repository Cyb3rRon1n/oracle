from __future__ import annotations

from server.narrator_ollama import OllamaNarrator
from server.rules import RulesIndex


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments):
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChunk:
    def __init__(self, content="", tool_calls=None, done=False):
        self.message = FakeMessage(content, tool_calls)
        self.done = done


class FakeOllamaClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        chunks = self._responses.pop(0)

        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()


def make_narrator() -> OllamaNarrator:
    return OllamaNarrator(rules=RulesIndex.load_default())


def noop_apply_update(update: dict) -> str:
    return "unexpected call"


async def test_narrate_streams_text_when_no_tool_use():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient(
        [
            [
                FakeChunk(content="You "),
                FakeChunk(content="enter "),
                FakeChunk(content="the tavern."),
                FakeChunk(done=True),
            ]
        ]
    )

    chunks = [c async for c in narrator.narrate([], "{}", "I enter the tavern", noop_apply_update)]

    assert "".join(chunks) == "You enter the tavern."
    assert len(narrator._client.calls) == 1


async def test_narrate_includes_system_prompt_and_history():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient([[FakeChunk(content="Okay.", done=True)]])
    history = [
        {"role": "user", "content": "I attack the goblin"},
        {"role": "assistant", "content": "You swing your sword."},
    ]

    [c async for c in narrator.narrate(history, "{}", "I check my inventory", noop_apply_update)]

    sent_messages = narrator._client.calls[0]["messages"]
    assert sent_messages[0]["role"] == "system"
    assert sent_messages[1] == history[0]
    assert sent_messages[2] == history[1]
    assert "I check my inventory" in sent_messages[3]["content"]


async def test_narrate_executes_lookup_rule_tool_and_continues():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient(
        [
            [
                FakeChunk(
                    tool_calls=[FakeToolCall("lookup_rule", {"category": "monster", "name": "goblin"})],
                ),
                FakeChunk(done=True),
            ],
            [
                FakeChunk(content="A goblin leaps out and attacks!"),
                FakeChunk(done=True),
            ],
        ]
    )

    chunks = [c async for c in narrator.narrate([], "{}", "I open the door", noop_apply_update)]

    assert "".join(chunks) == "A goblin leaps out and attacks!"
    assert len(narrator._client.calls) == 2

    second_call_messages = narrator._client.calls[1]["messages"]
    tool_result = second_call_messages[-1]
    assert tool_result["role"] == "tool"
    assert "Goblin" in tool_result["content"]
    assert '"ac": 15' in tool_result["content"]


async def test_narrate_routes_update_character_tool_to_callback():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient(
        [
            [
                FakeChunk(content="You take a hit."),
                FakeChunk(tool_calls=[FakeToolCall("update_character", {"hp_delta": -4})]),
                FakeChunk(done=True),
            ],
            [
                FakeChunk(content=" You reel."),
                FakeChunk(done=True),
            ],
        ]
    )

    received_updates = []

    def apply_update(update: dict) -> str:
        received_updates.append(update)
        return "Applied: HP -4 (now 6/10)."

    chunks = [c async for c in narrator.narrate([], "{}", "I stand my ground", apply_update)]

    assert "".join(chunks) == "You take a hit. You reel."
    assert received_updates == [{"hp_delta": -4}]

    second_call_messages = narrator._client.calls[1]["messages"]
    assert second_call_messages[-1] == {"role": "tool", "content": "Applied: HP -4 (now 6/10)."}


async def test_narrate_stops_after_max_tool_rounds_without_hanging():
    narrator = make_narrator()
    always_calls_tool = [
        FakeChunk(tool_calls=[FakeToolCall("lookup_rule", {"category": "monster", "name": "goblin"})]),
        FakeChunk(done=True),
    ]
    narrator._client = FakeOllamaClient([always_calls_tool for _ in range(4)])

    chunks = [c async for c in narrator.narrate([], "{}", "I keep fighting", noop_apply_update)]

    assert chunks == []
    assert len(narrator._client.calls) == 4
