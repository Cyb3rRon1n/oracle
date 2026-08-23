"""Protocol v2 additions (docs/protocol.md "Protocol v2 additions"): map
snapshots, clocks, the lorebook, scene_update emission, and the rolling
campaign summarizer."""

from __future__ import annotations

import json
from pathlib import Path

from shared.protocol import Envelope
from server.lorebook import Lorebook, _significant_words
from server.state import Clock, WorldState


# ── Map ──────────────────────────────────────────────────────────────────


def test_map_nodes_upsert_creates_node_with_hints():
    world = WorldState()
    world.apply_update({"map_nodes": [{"name": "Gate", "x": 3, "y": -2, "icon": "🚪"}]})
    assert world.map["nodes"] == [{"name": "Gate", "x": 3, "y": -2, "icon": "🚪"}]


def test_map_hints_create_the_location_in_the_graph_too():
    world = WorldState()
    world.apply_update({"map_nodes": [{"name": "Gate"}]})
    assert "Gate" in world.location_map


def test_map_coords_clamped_and_nullable_fields_preserved_on_upsert():
    world = WorldState()
    world.apply_update({"map_nodes": [{"name": "Gate", "x": 3, "y": -2}]})
    # absent x/y keeps existing coords; explicit null clears them
    world.apply_update({"map_nodes": [{"name": "Gate", "icon": "🚪"}]})
    node = world.map["nodes"][0]
    assert (node["x"], node["y"]) == (3, -2)
    world.apply_update({"map_nodes": [{"name": "Gate", "x": None, "y": None}]})
    node = world.map["nodes"][0]
    assert (node["x"], node["y"]) == (None, None)


def test_map_entries_without_a_name_are_ignored():
    world = WorldState()
    result = world.apply_update({"map_nodes": [{"icon": "x"}, "junk", 42]})
    assert result.startswith("No changes applied")
    assert world.map["nodes"] == []


def test_map_snapshot_derives_edges_from_adjacency_deduped():
    world = WorldState()
    world.apply_update({"add_location": "A", "connect_locations": ["A", "B"]})
    world.apply_update({"connect_locations": ["A", "B"]})  # duplicate ignored by adjacency itself
    names = [n["name"] for n in world.map["nodes"]]
    assert set(names) == {"A", "B"}
    assert world.map["edges"] == [["A", "B"]]


def test_map_rides_the_world_update_model_dump():
    world = WorldState()
    world.apply_update({"map_nodes": [{"name": "Gate", "x": 1, "y": 1}]})
    dump = world.model_dump()
    assert dump["map"]["nodes"][0]["name"] == "Gate"
    assert "location_map" in dump  # v1 compatibility until cutover


# ── Clocks ───────────────────────────────────────────────────────────────


def test_clock_add_clamps_segment_count():
    world = WorldState()
    world.apply_update({"add_clock": {"name": "Veil", "segments": 99}})
    assert world.clocks[0].segments == 12
    world.apply_update({"add_clock": {"name": "Doom", "segments": 1}})
    assert world.clocks[1].segments == 2  # protocol: segments clamped to 2-12


def test_clock_tick_fills_and_never_overflows():
    clock = Clock(name="Veil", segments=6)
    clock.tick(4)
    clock.tick(99)
    assert clock.filled == 6


def test_clock_tick_announces_complete_fill():
    world = WorldState()
    world.apply_update({"add_clock": {"name": "Veil", "segments": 3}})
    result = world.apply_update({"tick_clock": {"name": "Veil", "ticks": 3}})
    assert "filled completely" in result
    # a second tick of an already-full clock is not a new completion
    result = world.apply_update({"tick_clock": {"name": "Veil"}})
    assert "filled completely" not in result


def test_clock_set_clamps_and_remove_works():
    world = WorldState()
    world.apply_update({"add_clock": {"name": "Veil", "segments": 6}})
    world.apply_update({"set_clock": {"name": "Veil", "filled": 99}})
    assert world.clocks[0].filled == 6
    world.apply_update({"remove_clock": "Veil"})
    assert world.clocks == []


def test_clocks_surface_in_narrator_context():
    world = WorldState()
    world.apply_update({"add_clock": {"name": "Veil instability", "segments": 6}, "tick_clock": {"name": "Veil instability"}})
    context = world.narrator_context()
    assert "- Veil instability: 1/6" in context


# ── Lorebook ─────────────────────────────────────────────────────────────


def make_book(tmp_path: Path, files: dict[str, str]) -> Lorebook:
    paths = []
    for name, content in files.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return Lorebook.from_files(paths)


def test_markdown_sections_become_keyword_keyed_entries(tmp_path):
    book = make_book(
        tmp_path,
        {
            "a.md": "# Ashwren Gate\nKeywords: ashwren, warden\nShe guards the Veil.\n\n## The Wardstone\nIt flickers."
        },
    )
    assert len(book.entries) == 2
    gate = book.entries[0]
    assert gate.keys == ["ashwren", "warden"]
    assert "She guards the Veil." in gate.content
    wardstone = book.entries[1]
    assert "wardstone" in wardstone.keys


def test_json_entries_parse_directly(tmp_path):
    book = make_book(
        tmp_path,
        {
            "lore.json": json.dumps(
                {
                    "entries": [
                        {"title": "Veil Cost", "keys": ["veil"], "content": "Crossing costs a memory.", "priority": 5},
                        {"content": "Always-on premise.", "constant": True},
                    ]
                }
            )
        },
    )
    assert [e.constant for e in book.entries] == [False, True]
    assert book.entries[0].priority == 5


def test_invalid_json_degrades_to_whole_file_entry_not_silence(tmp_path):
    book = make_book(tmp_path, {"broken.json": "{not json"})
    assert len(book.entries) == 1
    assert book.entries[0].content.strip().startswith("{")


def test_csv_entries_read_keys_content_priority(tmp_path):
    book = make_book(tmp_path, {"lore.csv": "keys,content,priority\nveil,Costly crossing.,7\nashwren,Warden.,\n"})
    assert len(book.entries) == 2
    assert book.entries[0].priority == 7


def test_injection_hits_only_matching_keys_plus_constants(tmp_path):
    book = make_book(
        tmp_path,
        {
            "a.md": "# Gate\nKeywords: ashwren\nWarden text.",
            "b.json": json.dumps({"entries": [{"content": "Premise.", "constant": True}]}),
        },
    )
    block = book.injection_block("I approach ashwren.")
    assert "Warden text." in block and "Premise." in block
    unrelated = book.injection_block("totally unrelated chatter")
    assert "Warden text." not in unrelated and "Premise." in unrelated


def test_injection_budget_evicts_lowest_priority_first(tmp_path):
    entries = [
        {"keys": [f"k{i}"], "title": f"T{i}", "content": f"content-{i} " + "y" * 80, "priority": i}
        for i in range(5)
    ]
    book = make_book(tmp_path, {"lore.json": json.dumps({"entries": entries})})
    window = " ".join(f"k{i}" for i in range(5))
    block = book.injection_block(window, budget_chars=350)  # room for ~3 entries
    present = {f"content-{i}" for i in range(5) if f"content-{i}" in block}
    assert len(present) == 3
    assert present == {"content-2", "content-3", "content-4"}  # highest priorities survive


def test_injection_text_is_self_contained_carries_title(tmp_path):
    book = make_book(tmp_path, {"a.md": "# The Wardstone\nIt flickers faintly."})
    block = book.injection_block("wardstone")
    assert "[The Wardstone]" in block


def test_significant_words_drops_stopwords():
    assert _significant_words("The Warden of the Veil") == ["warden", "veil"]


# ── Engine wiring: scene_update, lore injection, clock announcements ─────


class SceneDM:
    """Two-phase stand-in: reports scene facts through the scene_sink the way
    a real decide phase would."""

    supports_scene_facts = True

    def __init__(self):
        self.scene_facts = None
        self.world_summary_seen = None

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None,
                      update_world=None, world_summary=None, scene_sink=None):
        self.world_summary_seen = world_summary
        if scene_sink is not None:
            scene_sink({"npcs_present": ["Ashwren"], "points_of_interest": ["the wardstone"],
                        "suggested_actions": ["Touch it", "Ask", "Run", "Wait", "Extra"]})
        yield "You face the warden."

    async def summarize(self, prior_summary, turns):
        return f"recap of {len(turns)} messages"


class LegacyDM:
    """Pre-v2 signature: no scene_sink kwarg at all - must keep working."""

    async def narrate(self, history, character_summary, action_text, apply_update,
                      request_roll=None, update_world=None, world_summary=None):
        yield "Plain narration."


def make_engine(dm):
    from server.engine import GameEngine
    from server.state import Session

    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env: Envelope):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env: Envelope):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, dm, broadcast, send_to, enable_opening_scene=False)
    return engine, session, received


async def join(engine, player_id="p1"):
    await engine.handle(Envelope(
        type="join_session", session_id="test-session", sender_id=player_id,
        payload={"player_name": "Thrain"},
    ))


async def act(engine, player_id="p1", text="I look around."):
    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": text},
    ))


async def test_turn_emits_scene_update_capped_at_four_actions():
    dm = SceneDM()
    engine, session, received = make_engine(dm)
    await join(engine)
    received.clear()
    await act(engine)
    scenes = [payload for kind, etype, payload in received if etype == "scene_update"]
    assert len(scenes) == 1
    payload = scenes[0]
    assert payload["npcs_present"] == ["Ashwren"]
    assert payload["suggested_actions"] == ["Touch it", "Ask", "Run", "Wait"]  # capped at 4
    assert "narration_id" in payload


async def test_legacy_dm_signature_still_works_without_scene_update():
    engine, session, received = make_engine(LegacyDM())
    await join(engine)
    received.clear()
    await act(engine)
    assert not any(etype == "scene_update" for _, etype, _ in received)
    narrations = [payload for _, etype, payload in received if etype == "log_entry" and payload.get("kind") == "narration"]
    assert any(n["text"] == "Plain narration." for n in narrations)


async def test_lorebook_selection_injects_hits_into_dm_context(tmp_path):
    lore_dir = tmp_path / "world_context"
    lore_dir.mkdir()
    (lore_dir / "gates.md").write_text("# Ashwren Gate\nKeywords: ashwren\nThe warden watches.", encoding="utf-8")

    from server.engine import GameEngine
    from server.state import Session

    session = Session(session_id="test-session")
    received: list[tuple] = []

    async def broadcast(env):
        received.append(("broadcast", env.type, env.payload))

    async def send_to(pid, env):
        received.append(("send_to", pid, env.type, env.payload))

    engine = GameEngine(session, SceneDM(), broadcast, send_to, enable_opening_scene=False)
    engine._world_context_dir = lore_dir

    # manifest request lists the file without content
    await engine.handle(Envelope(type="context_manifest_request", session_id="t", sender_id="p1", payload={}))
    manifests = [(pid, payload) for _, pid, etype, payload in received if etype == "context_manifest"]
    assert manifests and manifests[0][1]["files"][0]["name"] == "gates.md"

    # selection toggles it on and persists on the session
    received.clear()
    await engine.handle(Envelope(type="context_select", session_id="t", sender_id="p1",
                                 payload={"files": ["gates.md"]}))
    assert session.context_files == ["gates.md"]

    await join(engine)
    # No keyword anywhere -> entry stays out of the prompt entirely
    received.clear()
    await act(engine, text="I hum quietly.")
    assert "The warden watches." not in (engine._dm.world_summary_seen or "")

    # Keyword in the current action (or the recent play window it joins)
    # -> entry injected
    received.clear()
    await act(engine, text="I approach ashwren.")
    assert "The warden watches." in (engine._dm.world_summary_seen or "")


async def test_filled_clock_broadcasts_system_message_once():
    class ClockWorldDM:
        supports_scene_facts = False

        def __init__(self):
            self.ticked = False

        async def narrate(self, history, character_summary, action_text, apply_update,
                          request_roll=None, update_world=None, world_summary=None):
            if not self.ticked:
                self.ticked = True
                update_world({"tick_clock": {"name": "Veil", "ticks": 3}})
            yield "Time passes."

    engine, session, received = make_engine(ClockWorldDM())
    session.world.apply_update({"add_clock": {"name": "Veil", "segments": 3}, "tick_clock": {"name": "Veil", "ticks": 1}})
    await join(engine)
    received.clear()
    await act(engine)
    fills = [p for _, e, p in received if e == "system_message" and "fills" in str(p.get("text", ""))]
    assert len(fills) == 1
    received.clear()
    await act(engine)  # already full - no repeat announcement
    fills = [p for _, e, p in received if e == "system_message" and "fills" in str(p.get("text", ""))]
    assert fills == []


async def test_campaign_summary_rebuilds_every_ten_turns():
    class SummarizingDM(LegacyDM):
        def __init__(self):
            self.summarize_calls = 0

        async def summarize(self, prior_summary, turns):
            self.summarize_calls += 1
            return f"recap {self.summarize_calls}"

    dm = SummarizingDM()
    engine, session, received = make_engine(dm)
    await join(engine)
    for _ in range(9):
        await act(engine)
    assert dm.summarize_calls == 0  # not yet at the interval
    await act(engine)  # tenth resolved turn
    assert dm.summarize_calls == 1
    assert session.campaign_summary == "recap 1"


async def test_campaign_summary_flows_into_dm_context():
    class RecapDM(LegacyDM):
        async def narrate(self, history, character_summary, action_text, apply_update,
                          request_roll=None, update_world=None, world_summary=None):
            self.seen = world_summary
            yield "ok"

    dm = RecapDM()
    engine, session, _ = make_engine(dm)
    session.campaign_summary = "The party crossed the Veil."
    await join(engine)
    received_clear = True
    await act(engine)
    assert "Campaign so far: The party crossed the Veil." in (dm.seen or "")


# ── OpenAI-compatible backend (two-phase decide→narrate) ─────────────────


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        pass


async def test_openai_decide_retry_survives_one_unparseable_response(monkeypatch):
    from server.narrator_openai import OpenAINarrator

    narrator = OpenAINarrator(api_key="x")
    calls = {"n": 0}
    responses = [_FakeResponse("not json"), _FakeResponse('{"roll_requested": false}')]

    async def fake_post(url, json=None):
        calls["n"] += 1
        return responses[min(calls["n"] - 1, len(responses) - 1)]

    monkeypatch.setattr(narrator._client, "post", fake_post)
    data, _ = await narrator._decide([{"role": "user", "content": "hi"}], {})
    assert data == {"roll_requested": False}
    assert calls["n"] == 2  # one bad response, one retry, then success


async def test_openai_narrate_applies_changes_before_streaming_prose(monkeypatch):
    from server.narrator_openai import OpenAINarrator

    narrator = OpenAINarrator(api_key="x", world_updates=True)

    decide_payloads = [
        {
            "roll_requested": False,
            "mechanical_change": True,
            "target": "",
            "hp_delta": -3,
            "add_condition": "",
            "rest": "",
            "notes": "",
            "disposition": "",
            "cast_spell": "",
            "world_change": True,
            "location": "The Gate",
            "mood": "",
            "add_objective": "",
            "complete_objective": "",
            "add_location": "",
            "npcs_present": ["Ashwren"],
            "points_of_interest": [],
            "suggested_actions": ["a", "b", "c", "d", "e"],
        }
    ]

    async def fake_chat(messages, schema=None):
        # _chat returns the parsed response body (a dict), mirroring
        # httpx's response.json() in the real client.
        return {"choices": [{"message": {"content": json.dumps(decide_payloads[0])}}]}

    async def fake_stream(messages):
        yield "You take three damage."
        yield " The gate looms."

    applied = []
    sunk = []
    monkeypatch.setattr(narrator, "_chat", fake_chat)
    monkeypatch.setattr(narrator, "_chat_stream", fake_stream)

    chunks = []
    async for chunk in narrator.narrate(
        [],
        "{}",
        "I attack",
        lambda u: applied.append(u) or "applied",
        scene_sink=sunk.append,
    ):
        chunks.append(chunk)

    assert chunks == ["You take three damage.", " The gate looms."]
    assert applied == [{"target": "self", "hp_delta": -3}]
    assert len(sunk[0]["suggested_actions"]) <= 4
