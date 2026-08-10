from __future__ import annotations

import json

from server.narrator_ollama import (
    OLLAMA_TOOLS,
    STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA,
    STRUCTURED_OUTPUT_SCHEMA,
    STRUCTURED_OUTPUT_SYSTEM_PROMPT,
    OllamaNarrator,
    create_ollama_narrator,
)
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


class FakeChatResponse:
    """A single non-streamed response (chat(..., stream=False)) - the
    structured-output path's own shape, distinct from FakeChunk's
    streamed-iteration shape below."""

    def __init__(self, content: str):
        self.message = FakeMessage(content)


class FakeOllamaClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)

        # stream=False (the structured-output path) gets a single response
        # object back directly, not an async generator of chunks - the
        # real ollama.AsyncClient.chat() itself branches the same way on
        # this same parameter.
        if kwargs.get("stream") is False:
            return response

        async def gen():
            for chunk in response:
                yield chunk

        return gen()


def make_narrator() -> OllamaNarrator:
    # structured_output=False - these tests exercise the legacy native
    # tool-calling path specifically (FakeOllamaClient's streamed-chunk
    # shape below), now that structured_output defaults to True.
    return OllamaNarrator(rules=RulesIndex.load_default(), structured_output=False)


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


def test_request_roll_and_update_world_are_not_exposed_to_ollama_models():
    # Deliberate scoping (ROADMAP.md item 6): this session's investigation
    # found qwen2.5:7b/llama3.1:8b already miss the one existing tool on most
    # clearly-warranted turns, so request_roll/update_world are Anthropic-only
    # for now rather than adding more required calls on top of that.
    tool_names = {tool["function"]["name"] for tool in OLLAMA_TOOLS}
    assert tool_names == {"lookup_rule", "update_character"}
    assert "request_roll" not in tool_names
    assert "update_world" not in tool_names


def test_update_character_tool_exposes_notes_field_to_ollama_models():
    # notes is on the *shared* UPDATE_CHARACTER_TOOL, not a new tool, so
    # unlike request_roll/update_world it does apply to Ollama too.
    update_character = next(t for t in OLLAMA_TOOLS if t["function"]["name"] == "update_character")
    assert "notes" in update_character["function"]["parameters"]["properties"]


async def test_narrate_accepts_but_ignores_request_roll_callback():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient(
        [[FakeChunk(content="You wait.", done=True)]]
    )

    def unexpected_request_roll(update: dict) -> str:
        raise AssertionError("request_roll should never be invoked by OllamaNarrator yet")

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update, request_roll=unexpected_request_roll
        )
    ]

    assert "".join(chunks) == "You wait."


async def test_narrate_accepts_but_ignores_update_world_callback():
    narrator = make_narrator()
    narrator._client = FakeOllamaClient(
        [[FakeChunk(content="You wait.", done=True)]]
    )

    def unexpected_update_world(update: dict) -> str:
        raise AssertionError("update_world should never be invoked by OllamaNarrator yet")

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update, update_world=unexpected_update_world
        )
    ]

    assert "".join(chunks) == "You wait."


def make_structured_narrator() -> OllamaNarrator:
    return OllamaNarrator(rules=RulesIndex.load_default(), structured_output=True)


async def test_structured_narrate_yields_narration_and_applies_a_real_change():
    narrator = make_structured_narrator()
    payload = {
        "narration": "The bandit staggers back, wounded.",
        "mechanical_change": True,
        "target": "bandit",
        "hp_delta": -4,
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    chunks = [c async for c in narrator.narrate([], "{}", "I attack the bandit", record_apply_update)]

    assert "".join(chunks) == "The bandit staggers back, wounded."
    assert calls == [{"target": "bandit", "hp_delta": -4}]


async def test_structured_narrate_calls_ollama_with_the_real_schema_and_no_streaming():
    narrator = make_structured_narrator()
    payload = {"narration": "Nothing happens.", "mechanical_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [c async for c in narrator.narrate([], "{}", "I look around", noop_apply_update)]

    call = narrator._client.calls[0]
    assert call["format"] == STRUCTURED_OUTPUT_SCHEMA
    assert call["stream"] is False
    assert call["messages"][0] == {"role": "system", "content": STRUCTURED_OUTPUT_SYSTEM_PROMPT}


async def test_structured_narrate_no_mechanical_change_never_calls_apply_update():
    narrator = make_structured_narrator()
    payload = {"narration": "You glance around, finding nothing of note.", "mechanical_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    chunks = [c async for c in narrator.narrate([], "{}", "I look around", noop_apply_update)]

    assert "".join(chunks) == "You glance around, finding nothing of note."


async def test_structured_narrate_malformed_json_surfaces_raw_content_without_crashing():
    narrator = make_structured_narrator()
    narrator._client = FakeOllamaClient([FakeChatResponse("not valid json at all")])

    chunks = [c async for c in narrator.narrate([], "{}", "I do something", noop_apply_update)]

    assert "".join(chunks) == "not valid json at all"


async def test_structured_narrate_omits_zero_hp_delta_and_empty_condition():
    # A schema-conformant but "nothing to add" response (hp_delta: 0,
    # add_condition: "") shouldn't send those as real update_character
    # fields - character.apply_update() already treats a falsy hp_delta/
    # empty condition as a no-op, so this just confirms the structured
    # path doesn't pass them through needlessly either.
    narrator = make_structured_narrator()
    payload = {
        "narration": "You steady your stance.",
        "mechanical_change": True,
        "target": "self",
        "hp_delta": 0,
        "add_condition": "",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I brace myself", record_apply_update)]

    assert calls == [{"target": "self"}]


async def test_structured_narrate_no_roll_requested_stays_a_single_call():
    # The common case (README/ROADMAP framing: most turns have an obvious,
    # certain outcome) shouldn't pay the two-pass roll mechanism's extra
    # latency at all - roll_requested absent (same as explicitly False)
    # must never trigger a second call or touch request_roll.
    narrator = make_structured_narrator()
    payload = {"narration": "You open the door.", "mechanical_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    def unexpected_request_roll(update: dict) -> str:
        raise AssertionError("request_roll should never be invoked when roll_requested is false")

    chunks = [
        c
        async for c in narrator.narrate([], "{}", "I open the door", noop_apply_update, request_roll=unexpected_request_roll)
    ]

    assert "".join(chunks) == "You open the door."
    assert len(narrator._client.calls) == 1


async def test_structured_narrate_roll_requested_makes_a_real_roll_and_a_second_call():
    # The two-pass mechanism this test locks in: a first response deciding
    # a roll is needed must not have its own (unknown-outcome) narration
    # used - the real narration comes from a second call, made only after
    # request_roll has actually resolved a real result.
    narrator = make_structured_narrator()
    first_payload = {
        "narration": "placeholder - not used",
        "mechanical_change": False,
        "roll_requested": True,
        "roll_skill": "stealth",
        "roll_dc": 13,
    }
    second_payload = {
        "narration": "You slip past the guards without a sound.",
        "mechanical_change": False,
    }
    narrator._client = FakeOllamaClient(
        [FakeChatResponse(json.dumps(first_payload)), FakeChatResponse(json.dumps(second_payload))]
    )

    roll_calls: list[dict] = []

    def record_request_roll(update: dict) -> str:
        roll_calls.append(update)
        return "Rolled 1d20+4 (Stealth, +2 proficiency): 17 [17] vs DC 13 — success."

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I sneak past the guards", noop_apply_update, request_roll=record_request_roll
        )
    ]

    assert "".join(chunks) == "You slip past the guards without a sound."
    assert roll_calls == [{"skill": "stealth", "dc": 13}]
    assert len(narrator._client.calls) == 2


async def test_structured_narrate_second_call_uses_followup_schema_and_the_real_roll_result():
    narrator = make_structured_narrator()
    first_payload = {
        "narration": "placeholder",
        "mechanical_change": False,
        "roll_requested": True,
        "roll_ability": "dex",
    }
    second_payload = {"narration": "You fumble the attempt.", "mechanical_change": False}
    narrator._client = FakeOllamaClient(
        [FakeChatResponse(json.dumps(first_payload)), FakeChatResponse(json.dumps(second_payload))]
    )

    def record_request_roll(update: dict) -> str:
        return "Rolled 1d20+2 +2 DEX: 6 [4] vs DC 15 — failure."

    _ = [
        c
        async for c in narrator.narrate(
            [], "{}", "I try to climb the wall", noop_apply_update, request_roll=record_request_roll
        )
    ]

    second_call = narrator._client.calls[1]
    assert second_call["format"] == STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA
    assert "Rolled 1d20+2 +2 DEX: 6 [4] vs DC 15 — failure." in second_call["messages"][-1]["content"]


async def test_structured_narrate_roll_requested_applies_mechanical_change_from_second_call_only():
    # The first (roll-deciding) response's own mechanical_change must be
    # discarded even if the model set one - it was written blind, before
    # the real roll outcome existed, same as its narration.
    narrator = make_structured_narrator()
    first_payload = {
        "narration": "placeholder",
        "mechanical_change": True,
        "target": "self",
        "hp_delta": -99,
        "roll_requested": True,
        "roll_kind": "attack",
    }
    second_payload = {
        "narration": "Your blade lands true.",
        "mechanical_change": True,
        "target": "goblin",
        "hp_delta": -5,
    }
    narrator._client = FakeOllamaClient(
        [FakeChatResponse(json.dumps(first_payload)), FakeChatResponse(json.dumps(second_payload))]
    )

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [
        c
        async for c in narrator.narrate(
            [], "{}", "I attack the goblin", record_apply_update, request_roll=lambda u: "Rolled: 18 vs DC 12 — success."
        )
    ]

    assert calls == [{"target": "goblin", "hp_delta": -5}]


async def test_structured_narrate_roll_requested_without_a_request_roll_callback_falls_back_to_first_response():
    # A defensive path, not a normal case: if narrate() is ever called
    # without a request_roll callback at all, roll_requested=true can't
    # trigger a second call - falls back to the first response's own
    # (provisional) narration rather than crashing or hanging.
    narrator = make_structured_narrator()
    payload = {
        "narration": "You act on instinct.",
        "mechanical_change": False,
        "roll_requested": True,
        "roll_skill": "perception",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    chunks = [c async for c in narrator.narrate([], "{}", "I look for traps", noop_apply_update)]

    assert "".join(chunks) == "You act on instinct."
    assert len(narrator._client.calls) == 1


def test_default_structured_output_is_true():
    # A live 5-repeat qwen2.5:7b comparison found this roughly doubles
    # real tool-call correctness over native tool-calling - see
    # ROADMAP.md item 6. Confirms the constructor default itself, not
    # just create_ollama_narrator()'s env-var wiring below.
    assert OllamaNarrator(rules=RulesIndex.load_default())._structured_output is True


def test_create_ollama_narrator_defaults_to_structured_output(monkeypatch):
    monkeypatch.delenv("OLLAMA_STRUCTURED_OUTPUT", raising=False)
    assert create_ollama_narrator()._structured_output is True


def test_create_ollama_narrator_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("OLLAMA_STRUCTURED_OUTPUT", "0")
    assert create_ollama_narrator()._structured_output is False


def test_create_ollama_narrator_opt_out_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OLLAMA_STRUCTURED_OUTPUT", "False")
    assert create_ollama_narrator()._structured_output is False
