from __future__ import annotations

from scripts.live_reliability_check import SCENARIO, run_scenario, write_json


class ScriptedNarrator:
    """A NarratorBackend that plays back a fixed script, one entry per
    expected `narrate()` call, in order - lets scoring logic be tested
    without a live model."""

    def __init__(self, behaviors: list[dict]):
        self._behaviors = behaviors
        self.calls = 0

    async def narrate(self, history, character_summary, action_text, apply_update):
        behavior = self._behaviors[self.calls]
        self.calls += 1
        for update in behavior.get("updates", []):
            apply_update(update)
        for chunk in behavior.get("text_chunks", ["Something happens."]):
            yield chunk


def _all_correct_behaviors() -> list[dict]:
    return [
        {"updates": [{"target": "bandit", "hp_delta": -5, "max_hp": 10}], "text_chunks": ["Hit."]},
        {"updates": [{"target": "bandit", "hp_delta": -5}], "text_chunks": ["Down."]},
        {"updates": [], "text_chunks": ["Nothing here."]},
        {
            "updates": [{"target": "second bandit", "hp_delta": -5, "max_hp": 10}],
            "text_chunks": ["Hit again."],
        },
        {"updates": [{"target": "second bandit", "hp_delta": -3}], "text_chunks": ["Reels."]},
        {"updates": [{"target": "second bandit", "hp_delta": -10}], "text_chunks": ["Falls."]},
        {"updates": [], "text_chunks": ["Bleeding slows."]},
        {"updates": [], "text_chunks": ["Nothing valuable."]},
    ]


async def test_all_correct_scenario_scores_perfectly():
    narrator = ScriptedNarrator(_all_correct_behaviors())

    results = await run_scenario(narrator)

    assert len(results) == len(SCENARIO)
    scored = [r for r in results if r.correct is not None]
    assert scored, "expected at least one scored (non-ambiguous) turn"
    assert all(r.correct for r in scored)
    assert not any(r.leaked_text for r in results)

    ambiguous = [r for r in results if r.expected == "ambiguous"]
    assert all(r.correct is None for r in ambiguous)


async def test_missing_calls_are_scored_as_incorrect():
    behaviors = [{"updates": [], "text_chunks": ["Something happens."]} for _ in SCENARIO]
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(narrator)

    for r, turn in zip(results, SCENARIO):
        if turn["expected"] == "call":
            assert r.correct is False
            assert r.called is False
        elif turn["expected"] == "no_call":
            assert r.correct is True


async def test_wrong_target_is_scored_as_incorrect():
    behaviors = _all_correct_behaviors()
    # Turn 1 expects target "bandit" - mistarget it at the acting character instead.
    behaviors[0] = {"updates": [{"hp_delta": -5}], "text_chunks": ["Hit, but on the wrong sheet."]}
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(narrator)

    assert results[0].correct is False
    assert results[0].called_targets == ["self"]


async def test_leaked_tool_call_text_is_flagged_even_without_a_real_call():
    behaviors = [{"updates": [], "text_chunks": ["Something happens."]} for _ in SCENARIO]
    behaviors[0] = {
        "updates": [],
        "text_chunks": ['update_character {"target": "bandit", "hp_delta": -5}'],
    }
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(narrator)

    assert results[0].leaked_text is True
    assert results[0].called is False  # no real tool call reached apply_update
    assert results[0].correct is False


async def test_leaked_lookup_rule_text_is_also_flagged():
    # A live llama3.1:8b run leaked lookup_rule pseudo-calls, not just
    # update_character - the leak detector needs to catch both tool names.
    behaviors = [{"updates": [], "text_chunks": ["Something happens."]} for _ in SCENARIO]
    behaviors[0] = {
        "updates": [],
        "text_chunks": ['lookup_rule(category="monster", name="Goblin")'],
    }
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(narrator)

    assert results[0].leaked_text is True


async def test_write_json_round_trips(tmp_path):
    narrator = ScriptedNarrator(_all_correct_behaviors())
    results = await run_scenario(narrator)

    out_path = tmp_path / "report.json"
    write_json(results, "qwen2.5:7b", "ollama", None, out_path)

    import json

    report = json.loads(out_path.read_text())
    assert report["backend"] == "ollama"
    assert report["model"] == "qwen2.5:7b"
    assert report["max_history_messages"] is None
    assert len(report["turns"]) == len(SCENARIO)


async def test_run_scenario_passes_through_custom_max_history_messages():
    narrator = ScriptedNarrator(_all_correct_behaviors())

    results = await run_scenario(narrator, max_history_messages=4)

    assert len(results) == len(SCENARIO)
