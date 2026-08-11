from __future__ import annotations

import json

from scripts.live_world_reliability_check import (
    SCENARIO,
    aggregate_stats,
    run_scenario,
    run_stats,
    write_json,
    write_json_repeat,
)


class ScriptedWorldNarrator:
    """A NarratorBackend that plays back a fixed script, one entry per
    expected `narrate()` call, in order - lets this harness's own scoring
    logic be tested without a live model, the same precedent
    tests/test_live_reliability_check.py's own ScriptedNarrator already
    establishes for the update_character harness."""

    def __init__(self, behaviors: list[dict]):
        self._behaviors = behaviors
        self.calls = 0

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None):
        behavior = self._behaviors[self.calls]
        self.calls += 1
        world_update = behavior.get("world_update")
        if world_update and update_world is not None:
            update_world(world_update)
        for chunk in behavior.get("text_chunks", ["Something happens."]):
            yield chunk


def _all_correct_behaviors() -> list[dict]:
    return [
        {"world_update": {"location": "Millbrook"}, "text_chunks": ["You arrive."]},
        {"world_update": {"add_objective": "Find the missing goat"}, "text_chunks": ["A villager pleads for help."]},
        {"world_update": None, "text_chunks": ["Just idle chatter."]},
        {"world_update": {"location": "the old mill"}, "text_chunks": ["You head to the mill."]},
        {"world_update": {"complete_objective": "Find the missing goat"}, "text_chunks": ["The goat is found."]},
    ]


async def test_all_correct_scenario_scores_perfectly():
    narrator = ScriptedWorldNarrator(_all_correct_behaviors())

    results = await run_scenario(narrator)

    assert len(results) == len(SCENARIO)
    assert all(r.correct for r in results)
    assert results[0].location_after == "Millbrook"
    assert results[1].objectives_after == ["Find the missing goat"]
    assert results[4].completed_this_turn == ["Find the missing goat"]


async def test_missing_updates_are_scored_as_incorrect():
    behaviors = [{"world_update": None, "text_chunks": ["Nothing changes."]} for _ in SCENARIO]
    narrator = ScriptedWorldNarrator(behaviors)

    results = await run_scenario(narrator)

    for r, turn in zip(results, SCENARIO):
        if turn["expected"] == "no_change":
            assert r.correct is True
        else:
            assert r.correct is False
            assert r.world_update_fired is False


async def test_a_spurious_update_on_a_no_change_turn_is_scored_as_incorrect():
    # Turn 3 (the neutral "ask around town" turn) expects no change at
    # all - a real false-positive check, the same shape update_character's
    # own EXPECT_NO_CALL scoring already exercises.
    behaviors = _all_correct_behaviors()
    behaviors[2] = {"world_update": {"add_objective": "A hallucinated side quest"}, "text_chunks": ["Nothing notable."]}
    narrator = ScriptedWorldNarrator(behaviors)

    results = await run_scenario(narrator)

    assert results[2].correct is False
    assert results[2].world_update_fired is True


async def test_aggregate_stats_reports_mean_and_pooled_rate_across_repeats():
    perfect = await run_scenario(ScriptedWorldNarrator(_all_correct_behaviors()))
    zero_behaviors = [{"world_update": None, "text_chunks": ["Nothing."]} for _ in SCENARIO]
    zero = await run_scenario(ScriptedWorldNarrator(zero_behaviors))

    agg = aggregate_stats([perfect, zero])

    assert run_stats(perfect)["rate"] == 1.0
    # zero-behavior run still gets the one real no_change turn right.
    assert agg["total_scored"] == 2 * len(SCENARIO)
    assert agg["mean_rate"] == (1.0 + run_stats(zero)["rate"]) / 2


async def test_write_json_round_trips_a_single_run(tmp_path):
    results = await run_scenario(ScriptedWorldNarrator(_all_correct_behaviors()))
    out_path = tmp_path / "report.json"

    write_json(results, "test-model", "ollama", out_path)

    report = json.loads(out_path.read_text())
    assert report["model"] == "test-model"
    assert len(report["turns"]) == len(SCENARIO)


async def test_write_json_repeat_round_trips_an_aggregate(tmp_path):
    all_results = [
        await run_scenario(ScriptedWorldNarrator(_all_correct_behaviors())),
        await run_scenario(ScriptedWorldNarrator(_all_correct_behaviors())),
    ]
    out_path = tmp_path / "report.json"

    write_json_repeat(all_results, "test-model", "ollama", out_path)

    report = json.loads(out_path.read_text())
    assert report["repeat"] == 2
    assert report["aggregate"]["pooled_rate"] == 1.0
