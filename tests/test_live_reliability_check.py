from __future__ import annotations

from scripts.live_reliability_check import (
    FIELD_PARITY_CHARACTER_CLASS,
    FIELD_PARITY_SCENARIO,
    SCENARIO,
    aggregate_stats,
    run_scenario,
    run_stats,
    write_json,
    write_json_repeat,
)


class ScriptedNarrator:
    """A NarratorBackend that plays back a fixed script, one entry per
    expected `narrate()` call, in order - lets scoring logic be tested
    without a live model."""

    def __init__(self, behaviors: list[dict]):
        self._behaviors = behaviors
        self.calls = 0

    async def narrate(self, history, character_summary, action_text, apply_update, request_roll=None, update_world=None, world_summary=None):
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


async def test_a_recased_target_is_still_scored_correct():
    # A real scoring bug, found while analyzing a structured-output
    # experiment's results, not introduced by it: the real production
    # engine keys NPCs by target.casefold() (server/engine.py's
    # apply_update closure), so "Bandit" and "bandit" are the same
    # tracked NPC there - this harness's own scoring should agree, not
    # penalize a model for casing alone when the real engine wouldn't.
    behaviors = _all_correct_behaviors()
    behaviors[0] = {
        "updates": [{"target": "Bandit", "hp_delta": -5, "max_hp": 10}],
        "text_chunks": ["Hit."],
    }
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(narrator)

    assert results[0].called_targets == ["Bandit"]  # the real casing is still reported, unchanged
    assert results[0].correct is True  # but scored correct against the scenario's lowercase "bandit"


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


async def test_run_stats_on_all_correct_scenario():
    narrator = ScriptedNarrator(_all_correct_behaviors())
    results = await run_scenario(narrator)

    stats = run_stats(results)

    scored_turns = [t for t in SCENARIO if t["expected"] != "ambiguous"]
    assert stats["scored"] == len(scored_turns)
    assert stats["correct"] == len(scored_turns)
    assert stats["rate"] == 1.0
    assert stats["total_turns"] == len(SCENARIO)
    assert stats["leaked"] == 0


async def test_aggregate_stats_across_mixed_runs():
    # One perfect run, one run that misses every "call" turn - a fresh
    # ScriptedNarrator per run, since a single instance's behavior list is
    # only long enough for one pass through the 8-turn scenario.
    perfect = await run_scenario(ScriptedNarrator(_all_correct_behaviors()))
    all_missed = await run_scenario(
        ScriptedNarrator([{"updates": [], "text_chunks": ["Nothing happens."]} for _ in SCENARIO])
    )

    agg = aggregate_stats([perfect, all_missed])

    assert len(agg["per_run"]) == 2
    assert agg["per_run"][0]["rate"] == 1.0
    assert agg["per_run"][1]["rate"] is not None and agg["per_run"][1]["rate"] < 1.0
    # mean of per-run rates vs. pooled rate differ in general - both should
    # land strictly between the two runs' own rates for this mixed case.
    assert 0.0 < agg["mean_rate"] < 1.0
    assert 0.0 < agg["pooled_rate"] < 1.0
    assert agg["total_scored"] == agg["per_run"][0]["scored"] + agg["per_run"][1]["scored"]


def test_aggregate_stats_with_no_runs_reports_none():
    # Degenerate case: no runs at all shouldn't raise a divide-by-zero -
    # mean_rate/pooled_rate should just report None.
    agg = aggregate_stats([])

    assert agg["mean_rate"] is None
    assert agg["pooled_rate"] is None
    assert agg["total_scored"] == 0


async def test_write_json_repeat_round_trips(tmp_path):
    narrator1 = ScriptedNarrator(_all_correct_behaviors())
    narrator2 = ScriptedNarrator(_all_correct_behaviors())
    results1 = await run_scenario(narrator1)
    results2 = await run_scenario(narrator2)

    out_path = tmp_path / "repeat_report.json"
    write_json_repeat([results1, results2], "qwen3:8b", "ollama", None, out_path)

    import json

    report = json.loads(out_path.read_text())
    assert report["repeat"] == 2
    assert len(report["runs"]) == 2
    assert len(report["runs"][0]) == len(SCENARIO)
    assert report["aggregate"]["mean_rate"] == 1.0


# --- field-parity scenario (rest/notes/disposition/cast_spell) ---
# Separate from SCENARIO above on purpose - see live_reliability_check.py's
# module docstring and FIELD_PARITY_SCENARIO's own comment: never mixed
# with the combat scenario, so its own historical numbers stay comparable.


def _all_correct_field_parity_behaviors() -> list[dict]:
    return [
        {"updates": [{"target": "self", "hp_delta": -4}], "text_chunks": ["The dart bites in."]},
        {"updates": [{"target": "self", "rest": "long"}], "text_chunks": ["A full night's rest."]},
        {"updates": [{"target": "self", "cast_spell": "cure wounds"}], "text_chunks": ["A healing prayer."]},
        {
            "updates": [{"target": "pilgrim", "notes": "A grateful pilgrim seeking her family."}],
            "text_chunks": ["She thanks you warmly."],
        },
        {"updates": [{"target": "figure", "disposition": "hostile"}], "text_chunks": ["The figure attacks."]},
        {"updates": [], "text_chunks": ["The stars wheel overhead."]},
    ]


async def test_field_parity_scenario_scores_perfectly_when_every_field_is_set():
    narrator = ScriptedNarrator(_all_correct_field_parity_behaviors())

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert len(results) == len(FIELD_PARITY_SCENARIO)
    assert all(r.correct for r in results)


async def test_field_parity_rest_is_ambiguous_when_the_earlier_damage_call_never_landed():
    # A "long rest" at already-full HP is a real no-op (server/state.py) -
    # if the setup turn's own call was missed, the rest turn can't prove
    # anything either way and shouldn't be penalized.
    behaviors = _all_correct_field_parity_behaviors()
    behaviors[0] = {"updates": [], "text_chunks": ["Nothing happens."]}
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[0].correct is False  # the missed self-damage call is still a real miss
    assert results[1].correct is None  # but the rest turn itself is unscorable, not penalized


async def test_field_parity_rest_missing_is_scored_incorrect_when_healing_was_possible():
    behaviors = _all_correct_field_parity_behaviors()
    behaviors[1] = {"updates": [], "text_chunks": ["Nothing happens."]}
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[1].correct is False


async def test_field_parity_cast_spell_missing_is_scored_incorrect():
    behaviors = _all_correct_field_parity_behaviors()
    behaviors[2] = {"updates": [], "text_chunks": ["Nothing happens."]}
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[2].correct is False


async def test_field_parity_note_missing_is_scored_incorrect():
    behaviors = _all_correct_field_parity_behaviors()
    behaviors[3] = {"updates": [], "text_chunks": ["She says nothing memorable."]}
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[3].correct is False


async def test_field_parity_disposition_wrong_value_is_scored_incorrect():
    behaviors = _all_correct_field_parity_behaviors()
    behaviors[4] = {
        "updates": [{"target": "figure", "disposition": "neutral"}],
        "text_chunks": ["The figure watches, unreadable."],
    }
    narrator = ScriptedNarrator(behaviors)

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[4].correct is False


async def test_field_parity_no_call_control_turn_still_scores():
    narrator = ScriptedNarrator(_all_correct_field_parity_behaviors())

    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    assert results[5].correct is True


async def test_write_json_records_the_scenario_name(tmp_path):
    narrator = ScriptedNarrator(_all_correct_field_parity_behaviors())
    results = await run_scenario(
        narrator, scenario=FIELD_PARITY_SCENARIO, character_class=FIELD_PARITY_CHARACTER_CLASS
    )

    out_path = tmp_path / "field_parity_report.json"
    write_json(results, "qwen2.5:7b", "ollama", None, out_path, "field-parity")

    import json

    report = json.loads(out_path.read_text())
    assert report["scenario"] == "field-parity"
    assert len(report["turns"]) == len(FIELD_PARITY_SCENARIO)
