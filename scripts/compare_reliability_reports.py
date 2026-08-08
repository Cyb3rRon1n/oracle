"""Compares two or more saved live_reliability_check.py JSON reports side
by side, so "is this model worth switching to" is a table to glance at
instead of manually re-reading printed summaries and doing the arithmetic
by hand - the exact gap ROADMAP.md's reliability-investigation section
flagged: comparing models meant re-running the harness once per model and
eyeballing the output every time a new candidate showed up.

Deliberately scoped to comparing *already-saved* reports, not also driving
live runs across a list of models - that half of the original idea needs a
live backend (Ollama/Anthropic) to be worth anything, and this was built in
an environment with neither available. See ROADMAP.md for the honest split
between what's built here and what's still open.

Reuses live_reliability_check.py's own run_stats()/aggregate_stats() rather
than reimplementing what "correct" means a second time - single source of
truth for scoring stays in one place, same "don't duplicate scoring logic"
principle this workspace applies elsewhere (e.g. Vulcan's format_change(),
Atlas's _trend_summary()).

Usage:
    python -m scripts.compare_reliability_reports qwen25_7b.json qwen3_8b.json
    python -m scripts.compare_reliability_reports *.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.live_reliability_check import TurnResult, aggregate_stats, run_stats


def _turn_results_from_dicts(turn_dicts: list[dict]) -> list[TurnResult]:
    return [TurnResult(**d) for d in turn_dicts]


def load_report(path: Path) -> dict:
    """Normalizes a single-run or --repeat-shaped report into one
    consistent stats view. A --repeat report's "rate" is the pooled rate
    (total correct / total scored across every run) - the same number
    ROADMAP.md's own repeat experiments treat as the headline figure,
    with mean_rate carried alongside for the per-run-spread context a
    single pooled number can hide (see ROADMAP.md item 6's qwen2.5:7b
    repeat-5 entry: a flat 29% pooled rate hid real per-run variance
    underneath, 0/8 to 2/8)."""
    raw = json.loads(path.read_text())

    if "repeat" in raw:
        all_results = [_turn_results_from_dicts(run) for run in raw["runs"]]
        agg = aggregate_stats(all_results)
        per_run_stats = agg["per_run"]
        stats = {
            "scored": agg["total_scored"],
            "correct": agg["total_correct"],
            "rate": agg["pooled_rate"],
            "mean_rate": agg["mean_rate"],
            "real_calls": sum(s["real_calls"] for s in per_run_stats),
            "leaked": sum(s["leaked"] for s in per_run_stats),
            "total_turns": sum(s["total_turns"] for s in per_run_stats),
            "repeats": raw["repeat"],
        }
    else:
        results = _turn_results_from_dicts(raw["turns"])
        single = run_stats(results)
        stats = {**single, "mean_rate": single["rate"], "repeats": 1}

    return {
        "path": str(path),
        "backend": raw.get("backend"),
        "model": raw.get("model"),
        "max_history_messages": raw.get("max_history_messages"),
        **stats,
    }


def _rate_label(rate: float | None) -> str:
    return f"{100 * rate:.0f}%" if rate is not None else "n/a"


def format_comparison(reports: list[dict]) -> str:
    columns = ["model", "backend", "repeats", "rate", "real_calls/turns", "leaked/turns"]
    rows = [
        [
            r["model"] or "?",
            r["backend"] or "?",
            str(r["repeats"]),
            _rate_label(r["rate"]),
            f"{r['real_calls']}/{r['total_turns']}",
            f"{r['leaked']}/{r['total_turns']}",
        ]
        for r in reports
    ]
    widths = [max(len(columns[i]), *(len(row[i]) for row in rows)) for i in range(len(columns))]

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths))

    lines = [fmt_row(columns), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "reports", nargs="+", type=Path, help="Two or more JSON reports written by live_reliability_check.py --out"
    )
    args = parser.parse_args()

    loaded = [load_report(path) for path in args.reports]
    print(format_comparison(loaded))


if __name__ == "__main__":
    main()
