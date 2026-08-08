from __future__ import annotations

import json

from scripts.compare_reliability_reports import format_comparison, load_report
from scripts.live_reliability_check import SCENARIO, run_scenario, write_json, write_json_repeat
from tests.test_live_reliability_check import ScriptedNarrator, _all_correct_behaviors


async def test_load_report_on_a_single_run_report(tmp_path):
    narrator = ScriptedNarrator(_all_correct_behaviors())
    results = await run_scenario(narrator)
    path = tmp_path / "single.json"
    write_json(results, "qwen2.5:7b", "ollama", None, path)

    report = load_report(path)

    scored_turns = [t for t in SCENARIO if t["expected"] != "ambiguous"]
    assert report["model"] == "qwen2.5:7b"
    assert report["backend"] == "ollama"
    assert report["repeats"] == 1
    assert report["scored"] == len(scored_turns)
    assert report["correct"] == len(scored_turns)
    assert report["rate"] == 1.0
    assert report["mean_rate"] == 1.0
    assert report["leaked"] == 0


async def test_load_report_on_a_repeat_report_uses_pooled_rate(tmp_path):
    # One perfect run, one run that misses every "call" turn - pooled rate
    # should land strictly between the two runs' own rates, and mean_rate
    # should be carried alongside it, not silently dropped.
    perfect = await run_scenario(ScriptedNarrator(_all_correct_behaviors()))
    all_missed = await run_scenario(
        ScriptedNarrator([{"updates": [], "text_chunks": ["Nothing happens."]} for _ in SCENARIO])
    )
    path = tmp_path / "repeat.json"
    write_json_repeat([perfect, all_missed], "qwen3:8b", "ollama", None, path)

    report = load_report(path)

    assert report["model"] == "qwen3:8b"
    assert report["repeats"] == 2
    assert 0.0 < report["rate"] < 1.0
    assert 0.0 < report["mean_rate"] < 1.0
    assert report["total_turns"] == 2 * len(SCENARIO)


async def test_load_report_counts_leaked_text_across_runs(tmp_path):
    behaviors = _all_correct_behaviors()
    behaviors[0] = {
        "updates": [],
        "text_chunks": ['update_character {"target": "bandit", "hp_delta": -5}'],
    }
    leaked_run = await run_scenario(ScriptedNarrator(behaviors))
    clean_run = await run_scenario(ScriptedNarrator(_all_correct_behaviors()))
    path = tmp_path / "repeat_leak.json"
    write_json_repeat([leaked_run, clean_run], "llama3.1:8b", "ollama", None, path)

    report = load_report(path)

    assert report["leaked"] == 1


def test_format_comparison_renders_a_readable_table():
    reports = [
        {
            "model": "qwen2.5:7b", "backend": "ollama", "repeats": 5, "rate": 0.29,
            "real_calls": 3, "leaked": 8, "total_turns": 40,
        },
        {
            "model": "qwen3:8b", "backend": "ollama", "repeats": 5, "rate": 0.29,
            "real_calls": 30, "leaked": 0, "total_turns": 40,
        },
    ]

    table = format_comparison(reports)
    lines = table.splitlines()

    assert "model" in lines[0] and "rate" in lines[0]
    assert any("qwen2.5:7b" in line and "29%" in line for line in lines)
    assert any("qwen3:8b" in line and "30/40" in line for line in lines)


def test_format_comparison_handles_a_report_with_no_scored_turns():
    reports = [
        {"model": "broken:model", "backend": "ollama", "repeats": 1, "rate": None,
         "real_calls": 0, "leaked": 0, "total_turns": 8},
    ]

    table = format_comparison(reports)

    assert "n/a" in table


async def test_main_prints_a_comparison_for_real_files(tmp_path, capsys, monkeypatch):
    perfect = await run_scenario(ScriptedNarrator(_all_correct_behaviors()))
    path = tmp_path / "real.json"
    write_json(perfect, "qwen2.5:7b", "ollama", None, path)

    import sys

    from scripts.compare_reliability_reports import main

    monkeypatch.setattr(sys, "argv", ["compare_reliability_reports", str(path)])
    main()

    out = capsys.readouterr().out
    assert "qwen2.5:7b" in out
    assert "100%" in out
