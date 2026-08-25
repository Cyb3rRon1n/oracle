from __future__ import annotations

import json

from server.narrator_ollama import (
    MISSED_CHANGE_SCHEMA,
    OLLAMA_TOOLS,
    STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA,
    STRUCTURED_OUTPUT_ROLL_SCHEMA,
    STRUCTURED_OUTPUT_SCHEMA,
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
    # roll_requests defaults off (see OllamaNarrator's own default) - these
    # tests exercise the base structured-output path, byte-identical to
    # before OLLAMA_ROLL_REQUESTS existed. Roll-specific behavior below uses
    # make_structured_roll_narrator() instead.
    return OllamaNarrator(rules=RulesIndex.load_default(), structured_output=True, two_phase=False)


def make_structured_roll_narrator() -> OllamaNarrator:
    return OllamaNarrator(
        rules=RulesIndex.load_default(), structured_output=True, two_phase=False, roll_requests=True
    )


def make_structured_world_narrator() -> OllamaNarrator:
    return OllamaNarrator(
        rules=RulesIndex.load_default(), structured_output=True, two_phase=False, world_updates=True
    )


def make_structured_roll_and_world_narrator() -> OllamaNarrator:
    return OllamaNarrator(
        rules=RulesIndex.load_default(),
        structured_output=True,
        two_phase=False,
        roll_requests=True,
        world_updates=True,
    )


def noop_update_world(update: dict) -> str:
    return "unexpected call"


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
    # Compared against the narrator's own computed prompt, not the bare
    # module constant - the real one also has world-bible lore appended
    # (server/lore), present on every call regardless of history window size.
    assert call["messages"][0] == {"role": "system", "content": narrator._structured_system_prompt}


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


async def test_structured_narrate_omits_empty_rest_notes_disposition_cast_spell():
    # Same "schema-conformant but nothing to add" omission as the hp_delta/
    # add_condition test above, extended to the four fields added to close
    # ROADMAP.md's structured-output schema gap.
    narrator = make_structured_narrator()
    payload = {
        "narration": "You steady your stance.",
        "mechanical_change": True,
        "target": "self",
        "rest": "",
        "notes": "",
        "disposition": "",
        "cast_spell": "",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I brace myself", record_apply_update)]

    assert calls == [{"target": "self"}]


async def test_structured_narrate_applies_rest_notes_disposition_cast_spell():
    narrator = make_structured_narrator()
    payload = {
        "narration": "The wizard rests by the fire, then whispers an old prayer over the wounded scout.",
        "mechanical_change": True,
        "target": "scout",
        "rest": "long",
        "notes": "A grateful, loyal scout who now owes the party a favor.",
        "disposition": "friendly",
        "cast_spell": "cure wounds",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I tend to the scout", record_apply_update)]

    assert calls == [
        {
            "target": "scout",
            "rest": "long",
            "notes": "A grateful, loyal scout who now owes the party a favor.",
            "disposition": "friendly",
            "cast_spell": "cure wounds",
        }
    ]


async def test_structured_narrate_notes_alone_triggers_apply_update_without_mechanical_change():
    # notes/disposition/rest/cast_spell can each be the only real change on
    # a turn - mechanical_change's own schema description only promises
    # "HP, inventory, or conditions", so a model correctly leaving it false
    # for an NPC-introduction-only turn must still get its note recorded.
    narrator = make_structured_narrator()
    payload = {
        "narration": "The old innkeeper eyes you warily but says nothing more.",
        "mechanical_change": False,
        "target": "innkeeper",
        "notes": "Suspicious of strangers, but not hostile.",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I greet the innkeeper", record_apply_update)]

    assert calls == [{"target": "innkeeper", "notes": "Suspicious of strangers, but not hostile."}]


async def test_structured_narrate_disposition_alone_triggers_apply_update_without_mechanical_change():
    narrator = make_structured_narrator()
    payload = {
        "narration": "The bandit lowers his sword and raises his hands.",
        "mechanical_change": False,
        "target": "bandit",
        "disposition": "friendly",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I offer the bandit mercy", record_apply_update)]

    assert calls == [{"target": "bandit", "disposition": "friendly"}]


async def test_structured_narrate_rest_alone_triggers_apply_update_without_mechanical_change():
    narrator = make_structured_narrator()
    payload = {
        "narration": "You settle in by the fire and sleep through the night.",
        "mechanical_change": False,
        "target": "self",
        "rest": "long",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I make camp for the night", record_apply_update)]

    assert calls == [{"target": "self", "rest": "long"}]


async def test_structured_narrate_cast_spell_alone_triggers_apply_update_without_mechanical_change():
    narrator = make_structured_narrator()
    payload = {
        "narration": "You mutter the words and a mote of light flickers to life over your palm.",
        "mechanical_change": False,
        "target": "self",
        "cast_spell": "light",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_apply_update(update: dict) -> str:
        calls.append(update)
        return "ok"

    _ = [c async for c in narrator.narrate([], "{}", "I cast light", record_apply_update)]

    assert calls == [{"target": "self", "cast_spell": "light"}]


async def test_structured_narrate_roll_requests_off_ignores_roll_fields_entirely():
    # OLLAMA_ROLL_REQUESTS defaults off - even a payload that (implausibly,
    # since the off-path schema doesn't offer the field at all) sets
    # roll_requested must never trigger a second call or touch
    # request_roll while the flag is off. Confirms "off" really means off,
    # not just "the model chose not to use it this time".
    narrator = make_structured_narrator()
    payload = {"narration": "You open the door.", "mechanical_change": False, "roll_requested": True}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    def unexpected_request_roll(update: dict) -> str:
        raise AssertionError("request_roll should never be invoked when OLLAMA_ROLL_REQUESTS is off")

    chunks = [
        c
        async for c in narrator.narrate([], "{}", "I open the door", noop_apply_update, request_roll=unexpected_request_roll)
    ]

    assert "".join(chunks) == "You open the door."
    assert len(narrator._client.calls) == 1


async def test_structured_narrate_roll_requests_on_uses_the_roll_schema_and_prompt():
    narrator = make_structured_roll_narrator()
    payload = {"narration": "Nothing happens.", "mechanical_change": False, "roll_requested": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [c async for c in narrator.narrate([], "{}", "I look around", noop_apply_update)]

    call = narrator._client.calls[0]
    assert call["format"] == STRUCTURED_OUTPUT_ROLL_SCHEMA
    assert call["messages"][0] == {"role": "system", "content": narrator._structured_roll_system_prompt}


async def test_structured_narrate_no_roll_requested_stays_a_single_call():
    # The common case (README/ROADMAP framing: most turns have an obvious,
    # certain outcome) shouldn't pay the two-pass roll mechanism's extra
    # latency at all, even with roll_requests on - roll_requested=false
    # must never trigger a second call or touch request_roll.
    narrator = make_structured_roll_narrator()
    payload = {"narration": "You open the door.", "mechanical_change": False, "roll_requested": False}
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
    narrator = make_structured_roll_narrator()
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
    narrator = make_structured_roll_narrator()
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
    narrator = make_structured_roll_narrator()
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
    narrator = make_structured_roll_narrator()
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


async def test_structured_narrate_world_updates_off_ignores_world_fields_entirely():
    narrator = make_structured_narrator()
    payload = {
        "narration": "You step into the great hall.",
        "mechanical_change": False,
        "world_change": True,
        "location": "Great Hall",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I enter the hall", noop_apply_update, update_world=noop_update_world
        )
    ]

    assert "".join(chunks) == "You step into the great hall."


async def test_structured_narrate_world_updates_uses_the_world_schema_and_prompt():
    narrator = make_structured_world_narrator()
    payload = {"narration": "Nothing changes.", "mechanical_change": False, "world_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [c async for c in narrator.narrate([], "{}", "I wait", noop_apply_update)]

    call = narrator._client.calls[0]
    assert "world_change" in call["format"]["properties"]
    assert "world_change" in call["format"]["required"]
    assert "location" in call["format"]["properties"]
    assert "mood" in call["format"]["properties"]
    assert "You must also track world_change" in call["messages"][0]["content"]


async def test_structured_narrate_includes_world_summary_when_given():
    # Given directly in the same call's own prompt, not left for the model
    # to recall from history - tests a real, if ultimately unproven,
    # hypothesis for complete_objective's own 0% measured reliability
    # (ROADMAP.md's update_world investigation): that recalling an
    # objective's exact text was the bottleneck. Real testing found that
    # wasn't the case even with the text sitting right here - see
    # WORLD_UPDATE_PROMPT_ADDENDUM's own comment (server/narrator_ollama.py)
    # for the full writeup. Kept as real, defensible context regardless.
    narrator = make_structured_world_narrator()
    payload = {"narration": "Nothing changes.", "mechanical_change": False, "world_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [
        c async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update,
            world_summary="Current location: Millbrook\nActive objectives:\n- Find the missing goat",
        )
    ]

    call = narrator._client.calls[0]
    assert "World state:" in call["messages"][-1]["content"]
    assert "Find the missing goat" in call["messages"][-1]["content"]


async def test_structured_narrate_includes_the_summary_even_when_world_updates_is_off():
    # Only meaningful when world_updates is actually on - passing it
    # otherwise would describe schema fields this call doesn't even
    # expose, pure noise for a narrator that isn't tracking world state.
    narrator = OllamaNarrator(model="m", rules=RulesIndex.load_default(), structured_output=True, two_phase=False)
    payload = {"narration": "Nothing changes.", "mechanical_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [
        c async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update, world_summary="Tracked NPCs:\n- Bandit: HP 3/10"
        )
    ]

    call = narrator._client.calls[0]
    assert "World state:" in call["messages"][-1]["content"]
    assert "Bandit: HP 3/10" in call["messages"][-1]["content"]
    assert "world_change" not in call["format"]["properties"]


async def test_structured_narrate_empty_world_summary_sends_no_world_state_section():
    narrator = OllamaNarrator(model="m", rules=RulesIndex.load_default(), structured_output=True, two_phase=False)
    payload = {"narration": "Nothing changes.", "mechanical_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    _ = [
        c async for c in narrator.narrate([], "{}", "I wait", noop_apply_update, world_summary="")
    ]

    call = narrator._client.calls[0]
    assert "World state:" not in call["messages"][-1]["content"]


async def test_structured_narrate_world_change_applies_a_real_update():
    narrator = make_structured_world_narrator()
    payload = {
        "narration": "You step into the great hall.",
        "mechanical_change": False,
        "world_change": True,
        "location": "Great Hall",
        "mood": "tense",
        "add_objective": "Find the missing heirloom",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    calls: list[dict] = []

    def record_update_world(update: dict) -> str:
        calls.append(update)
        return "ok"

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I enter the hall", noop_apply_update, update_world=record_update_world
        )
    ]

    assert "".join(chunks) == "You step into the great hall."
    assert calls == [{"location": "Great Hall", "mood": "tense", "add_objective": "Find the missing heirloom"}]


async def test_structured_narrate_no_world_change_never_calls_update_world():
    narrator = make_structured_world_narrator()
    payload = {"narration": "Nothing notable happens.", "mechanical_change": False, "world_change": False}
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    chunks = [
        c
        async for c in narrator.narrate([], "{}", "I look around", noop_apply_update, update_world=noop_update_world)
    ]

    assert "".join(chunks) == "Nothing notable happens."


async def test_structured_narrate_roll_and_world_together_applies_both_from_second_call():
    # Combined flags: the first (roll-deciding) response's own world_change
    # must be discarded too, same as its narration/mechanical_change - all
    # written blind, before the real roll outcome existed.
    narrator = make_structured_roll_and_world_narrator()
    first_payload = {
        "narration": "placeholder", "mechanical_change": False, "roll_requested": True, "roll_kind": "check",
        "world_change": True, "location": "should not be used",
    }
    second_payload = {
        "narration": "You persuade the guard to let you through.",
        "mechanical_change": False,
        "world_change": True,
        "add_objective": "Find the missing heirloom",
    }
    narrator._client = FakeOllamaClient(
        [FakeChatResponse(json.dumps(first_payload)), FakeChatResponse(json.dumps(second_payload))]
    )

    world_calls: list[dict] = []

    def record_update_world(update: dict) -> str:
        world_calls.append(update)
        return "ok"

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I try to talk my way past the guard", noop_apply_update,
            request_roll=lambda u: "Rolled: 18 vs DC 12 — success.", update_world=record_update_world,
        )
    ]

    assert "".join(chunks) == "You persuade the guard to let you through."
    assert world_calls == [{"add_objective": "Find the missing heirloom"}]


async def test_structured_narrate_roll_and_world_together_no_roll_fired_uses_first_call():
    # Combined flags, but the model decides no roll is needed this turn -
    # only one call happens, and that call's own world_change is the real
    # (not discarded) one, since there's no second call to supersede it.
    narrator = make_structured_roll_and_world_narrator()
    payload = {
        "narration": "You step into the great hall.",
        "mechanical_change": False,
        "roll_requested": False,
        "world_change": True,
        "location": "Great Hall",
    }
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps(payload))])

    world_calls: list[dict] = []

    def record_update_world(update: dict) -> str:
        world_calls.append(update)
        return "ok"

    chunks = [
        c
        async for c in narrator.narrate(
            [], "{}", "I enter the hall", noop_apply_update, update_world=record_update_world
        )
    ]

    assert "".join(chunks) == "You step into the great hall."
    assert world_calls == [{"location": "Great Hall"}]
    assert len(narrator._client.calls) == 1


def make_two_phase_narrator(fact_ledger: bool = True) -> OllamaNarrator:
    return OllamaNarrator(
        rules=RulesIndex.load_default(),
        structured_output=True,
        two_phase=True,
        fact_ledger=fact_ledger,
    )


def _decide_payload(**overrides) -> str:
    payload = {"mechanical_change": False}
    payload.update(overrides)
    return json.dumps(payload)


async def test_two_phase_fact_ledger_adds_new_facts_to_schema_and_prompt():
    narrator = make_two_phase_narrator()
    narrator._client = FakeOllamaClient([
        FakeChatResponse(_decide_payload()),
        [FakeChunk("Decided prose.")],
    ])

    _ = [c async for c in narrator.narrate([], "{}", "I wait", noop_apply_update)]

    call = narrator._client.calls[0]
    assert "new_facts" in call["format"]["properties"]
    assert "new_facts" in call["messages"][0]["content"]


async def test_two_phase_fact_ledger_sinks_new_facts_and_keeps_them_out_of_prose_context():
    narrator = make_two_phase_narrator()
    narrator._client = FakeOllamaClient([
        FakeChatResponse(_decide_payload(new_facts=["Marta the innkeeper owes Rook 10 gold."])),
        [FakeChunk("Decided prose.")],
    ])
    sunk: list[list[str]] = []

    _ = [
        c
        async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update, fact_sink=sunk.append
        )
    ]

    assert sunk == [["Marta the innkeeper owes Rook 10 gold."]]
    narrate_call = narrator._client.calls[1]
    assert "new_facts" not in narrate_call["messages"][-1]["content"]
    assert "Marta" not in narrate_call["messages"][-1]["content"]


async def test_two_phase_fact_ledger_off_omits_field_prompt_and_sink():
    narrator = make_two_phase_narrator(fact_ledger=False)
    narrator._client = FakeOllamaClient([
        FakeChatResponse(_decide_payload(new_facts=["Should be ignored."])),
        [FakeChunk("Decided prose.")],
    ])
    sunk: list[list[str]] = []

    _ = [
        c
        async for c in narrator.narrate(
            [], "{}", "I wait", noop_apply_update, fact_sink=sunk.append
        )
    ]

    call = narrator._client.calls[0]
    assert "new_facts" not in call["format"]["properties"]
    assert "new_facts" not in call["messages"][0]["content"]
    assert narrator.supports_fact_ledger is False
    assert sunk == []


def test_create_ollama_narrator_defaults_fact_ledger_off(monkeypatch):
    monkeypatch.delenv("OLLAMA_FACT_LEDGER", raising=False)
    narrator = create_ollama_narrator()

    assert narrator.supports_fact_ledger is False


def test_create_ollama_narrator_respects_fact_ledger_opt_in(monkeypatch):
    monkeypatch.setenv("OLLAMA_FACT_LEDGER", "1")
    narrator = create_ollama_narrator()

    assert narrator.supports_fact_ledger is True


def test_default_structured_output_is_true():
    # A live 5-repeat qwen2.5:7b comparison found this roughly doubles
    # real tool-call correctness over native tool-calling - see
    # ROADMAP.md item 6. Confirms the constructor default itself, not
    # just create_ollama_narrator()'s env-var wiring below.
    assert OllamaNarrator(rules=RulesIndex.load_default())._structured_output is True


def test_few_shot_example_defaults_off():
    # Off by default - a real, untested candidate (ROADMAP.md item 6),
    # not yet validated the way structured_output itself was before its
    # own default flipped.
    narrator = OllamaNarrator(rules=RulesIndex.load_default())
    assert "Worked example" not in narrator._structured_system_prompt


def test_few_shot_example_opt_in_augments_the_base_structured_prompt():
    narrator = OllamaNarrator(rules=RulesIndex.load_default(), few_shot_example=True)
    assert "Worked example" in narrator._structured_system_prompt
    assert '"target": "bandit"' in narrator._structured_system_prompt


def test_few_shot_example_does_not_reach_the_roll_or_followup_prompts():
    # Deliberately scoped to only the base structured prompt for this
    # first test (STRUCTURED_OUTPUT_FEW_SHOT_EXAMPLE's own docstring) -
    # not yet extended to the roll-deciding or follow-up variants.
    narrator = OllamaNarrator(rules=RulesIndex.load_default(), roll_requests=True, few_shot_example=True)
    assert "Worked example" not in narrator._structured_roll_system_prompt
    assert "Worked example" not in narrator._structured_followup_system_prompt


def test_hardened_rules_defaults_off():
    narrator = OllamaNarrator(rules=RulesIndex.load_default())
    assert "Rule integrity" not in narrator._structured_system_prompt


def test_hardened_rules_opt_in_reaches_every_prompt_variant():
    # Roll turns are exactly where rhetorical pressure lands - the base
    # prompt alone wouldn't cover the failure mode, so the addendum rides
    # all four variants.
    narrator = OllamaNarrator(rules=RulesIndex.load_default(), hardened_rules=True)
    for prompt in (
        narrator._tool_calling_system_prompt,
        narrator._structured_system_prompt,
        narrator._structured_roll_system_prompt,
        narrator._structured_followup_system_prompt,
    ):
        assert "Rule integrity" in prompt


def test_create_ollama_narrator_defaults_to_structured_output(monkeypatch):
    monkeypatch.delenv("OLLAMA_STRUCTURED_OUTPUT", raising=False)
    assert create_ollama_narrator()._structured_output is True


def test_create_ollama_narrator_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("OLLAMA_STRUCTURED_OUTPUT", "0")
    assert create_ollama_narrator()._structured_output is False


def test_create_ollama_narrator_opt_out_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OLLAMA_STRUCTURED_OUTPUT", "False")
    assert create_ollama_narrator()._structured_output is False


def test_default_roll_requests_is_false():
    # Unlike structured_output above - real signal from live spot-checks,
    # but not yet validated at the same rigor. See
    # STRUCTURED_OUTPUT_ROLL_SCHEMA's own docstring and ROADMAP.md.
    assert OllamaNarrator(rules=RulesIndex.load_default())._roll_requests is False


def test_create_ollama_narrator_defaults_roll_requests_off(monkeypatch):
    monkeypatch.delenv("OLLAMA_ROLL_REQUESTS", raising=False)
    assert create_ollama_narrator()._roll_requests is False


def test_create_ollama_narrator_respects_explicit_roll_requests_opt_in(monkeypatch):
    monkeypatch.setenv("OLLAMA_ROLL_REQUESTS", "1")
    assert create_ollama_narrator()._roll_requests is True


def test_create_ollama_narrator_roll_requests_opt_in_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OLLAMA_ROLL_REQUESTS", "True")
    assert create_ollama_narrator()._roll_requests is True


def test_default_world_updates_is_false():
    assert OllamaNarrator(rules=RulesIndex.load_default())._world_updates is False


def test_create_ollama_narrator_defaults_world_updates_off(monkeypatch):
    monkeypatch.delenv("OLLAMA_WORLD_UPDATES", raising=False)
    assert create_ollama_narrator()._world_updates is False


def test_create_ollama_narrator_respects_explicit_world_updates_opt_in(monkeypatch):
    monkeypatch.setenv("OLLAMA_WORLD_UPDATES", "1")
    assert create_ollama_narrator()._world_updates is True


def test_create_ollama_narrator_world_updates_opt_in_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OLLAMA_WORLD_UPDATES", "True")
    assert create_ollama_narrator()._world_updates is True


async def test_check_missed_change_applies_a_real_correction():
    narrator = make_structured_narrator()
    response = FakeChatResponse(
        json.dumps({"mechanical_change": True, "target": "bandit", "hp_delta": -6})
    )
    narrator._client = FakeOllamaClient([response])

    received_updates = []

    def apply_update(update: dict) -> str:
        received_updates.append(update)
        return "Applied."

    corrected = await narrator.check_missed_change("Your blade cuts deep into the bandit.", "{}", apply_update)

    assert corrected is True
    assert received_updates == [{"target": "bandit", "hp_delta": -6}]
    call = narrator._client.calls[0]
    assert call["format"] == MISSED_CHANGE_SCHEMA
    assert "narration" not in call["format"]["properties"]


async def test_check_missed_change_applies_rest_notes_disposition_cast_spell():
    # Same _outcome_update mapping the main narrate() path uses (see
    # test_structured_narrate_applies_rest_notes_disposition_cast_spell) -
    # confirms it's genuinely shared, not a second copy that could drift.
    narrator = make_structured_narrator()
    response = FakeChatResponse(
        json.dumps(
            {
                "mechanical_change": True,
                "target": "self",
                "rest": "short",
                "notes": "Exhausted but resolute.",
                "disposition": "",
                "cast_spell": "cure wounds",
            }
        )
    )
    narrator._client = FakeOllamaClient([response])

    received_updates = []

    def apply_update(update: dict) -> str:
        received_updates.append(update)
        return "Applied."

    await narrator.check_missed_change("You catch your breath and murmur a healing prayer.", "{}", apply_update)

    assert received_updates == [
        {"target": "self", "rest": "short", "notes": "Exhausted but resolute.", "cast_spell": "cure wounds"}
    ]


async def test_check_missed_change_applies_a_notes_only_correction_without_mechanical_change():
    # The review path's gate must match the main narrate() path's own
    # broadened semantics: notes/disposition/rest/cast_spell each count as a
    # real correction even when mechanical_change is false.
    narrator = make_structured_narrator()
    response = FakeChatResponse(
        json.dumps({"mechanical_change": False, "target": "bandit", "notes": "A greedy toll-keeper."})
    )
    narrator._client = FakeOllamaClient([response])

    received_updates = []

    def apply_update(update: dict) -> str:
        received_updates.append(update)
        return "Applied."

    corrected = await narrator.check_missed_change("The bandit grins and names his price.", "{}", apply_update)

    assert corrected is True
    assert received_updates == [{"target": "bandit", "notes": "A greedy toll-keeper."}]
    review_prompt = narrator._client.calls[0]["messages"][0]["content"]
    for field in ("rest", "notes", "disposition", "cast_spell"):
        assert field in review_prompt


async def test_check_missed_change_returns_false_when_the_dm_declines_to_correct():
    narrator = make_structured_narrator()
    response = FakeChatResponse(json.dumps({"mechanical_change": False}))
    narrator._client = FakeOllamaClient([response])

    def unexpected_apply_update(update: dict) -> str:
        raise AssertionError("should never be called - the DM found nothing to correct")

    corrected = await narrator.check_missed_change("You walk into the empty room.", "{}", unexpected_apply_update)

    assert corrected is False


async def test_check_missed_change_returns_false_on_malformed_json():
    narrator = make_structured_narrator()
    narrator._client = FakeOllamaClient([FakeChatResponse("not valid json")])

    def unexpected_apply_update(update: dict) -> str:
        raise AssertionError("should never be called on a parse failure")

    corrected = await narrator.check_missed_change("Something happened.", "{}", unexpected_apply_update)

    assert corrected is False


async def test_check_missed_change_is_a_no_op_for_the_legacy_tool_calling_path():


    narrator = make_narrator()

    def unexpected_apply_update(update: dict) -> str:
        raise AssertionError("should never be called on the legacy tool-calling path")

    corrected = await narrator.check_missed_change("Something happened.", "{}", unexpected_apply_update)

    assert corrected is False


async def test_propose_correction_returns_best_guess_update_without_applying():
    narrator = make_structured_narrator()
    response = FakeChatResponse(
        json.dumps({"mechanical_change": True, "target": "bandit", "hp_delta": -6})
    )
    narrator._client = FakeOllamaClient([response])

    proposed = await narrator.propose_correction("Your blade cuts deep into the bandit.", "{}")

    assert proposed == {"target": "bandit", "hp_delta": -6}
    call = narrator._client.calls[0]
    assert call["format"] == MISSED_CHANGE_SCHEMA


async def test_propose_correction_returns_a_rest_only_proposal_without_mechanical_change():
    # Same broadened gate as check_missed_change: a rest-only hypothesis is
    # a real proposal, not discarded because mechanical_change is false.
    narrator = make_structured_narrator()
    response = FakeChatResponse(
        json.dumps({"mechanical_change": False, "target": "self", "rest": "long"})
    )
    narrator._client = FakeOllamaClient([response])

    proposed = await narrator.propose_correction("You make camp and sleep until dawn.", "{}")

    assert proposed == {"target": "self", "rest": "long"}
    review_prompt = narrator._client.calls[0]["messages"][0]["content"]
    for field in ("rest", "notes", "disposition", "cast_spell"):
        assert field in review_prompt


async def test_propose_correction_returns_none_when_dm_has_no_guess():
    narrator = make_structured_narrator()
    narrator._client = FakeOllamaClient([FakeChatResponse(json.dumps({"mechanical_change": False}))])

    proposed = await narrator.propose_correction("You walk into the empty room.", "{}")

    assert proposed is None


async def test_propose_correction_returns_none_on_malformed_json():
    narrator = make_structured_narrator()
    narrator._client = FakeOllamaClient([FakeChatResponse("not valid json")])

    proposed = await narrator.propose_correction("Something happened.", "{}")

    assert proposed is None


async def test_propose_correction_is_a_no_op_for_the_legacy_tool_calling_path():
    narrator = make_narrator()

    proposed = await narrator.propose_correction("Something happened.", "{}")

    assert proposed is None
