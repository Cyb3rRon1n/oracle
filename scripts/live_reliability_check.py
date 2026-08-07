"""Live tool-call reliability check for a NarratorBackend.

Runs a fixed 8-turn combat scenario through the real GameEngine against a
real, live backend (Ollama or Anthropic) - not a mock - and reports whether
`update_character` actually fired when narration clearly warranted it,
whether it targeted the right sheet (acting character vs. named NPC), and
whether the model leaked pseudo-tool-call text into narration instead of
using the real tool-call channel.

Background: ROADMAP.md items 4-6. This replaces the one-off scratchpad
scripts used during that investigation with a reusable, version-controlled
tool so the same check can be re-run consistently across models, backends,
and candidate fixes.

The scenario below is a faithful reconstruction of the shape described in
ROADMAP.md item 4's "second pass" (two sequential bandits, a mix of
call/no-call/ambiguous turns) - the exact original action strings were only
paraphrased there, not preserved verbatim, so this is not a byte-exact
replay of that session.

Usage:
    python -m scripts.live_reliability_check --backend ollama --model qwen2.5:7b
    python -m scripts.live_reliability_check --backend anthropic --out results.json
    python -m scripts.live_reliability_check --model qwen3:8b --repeat 3  # aggregate over 3 runs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from server.engine import GameEngine
from server.state import Session
from shared.protocol import Envelope

SESSION_ID = "reliability-check"
PLAYER_ID = "player-1"
PLAYER_NAME = "Torvin Ironheart"

EXPECT_CALL = "call"
EXPECT_NO_CALL = "no_call"
EXPECT_AMBIGUOUS = "ambiguous"

SCENARIO = [
    {
        "action": "I draw my sword and attack the bandit blocking the doorway.",
        "expected": EXPECT_CALL,
        "target": "bandit",
    },
    {
        "action": "I press the attack, striking the bandit again.",
        "expected": EXPECT_CALL,
        "target": "bandit",
    },
    {
        "action": "I glance around the room for anything useful before moving on.",
        "expected": EXPECT_NO_CALL,
        "target": None,
    },
    {
        "action": "A second bandit rushes in — I meet him with a slash of my blade.",
        "expected": EXPECT_CALL,
        "target": "second bandit",
    },
    {
        "action": "I follow up with another strike against the second bandit.",
        "expected": EXPECT_CALL,
        "target": "second bandit",
    },
    {
        "action": "I finish him off with a final blow.",
        "expected": EXPECT_CALL,
        "target": "second bandit",
    },
    {
        "action": "I press a cloth against my wound to try to stop the bleeding.",
        "expected": EXPECT_AMBIGUOUS,
        "target": "self",
    },
    {
        "action": "I search the bodies for anything valuable.",
        "expected": EXPECT_NO_CALL,
        "target": None,
    },
]

# Catches the leaked-pseudo-tool-call failure mode documented in ROADMAP.md
# item 5 stage two: the model writing its tool-call intent as visible prose
# (e.g. `update_character {"target": "Bandit", "hp_delta": -8}` or
# `lookup_rule(category="monster", name="Goblin")`) instead of invoking the
# real function-calling channel. Covers both tools - a live llama3.1:8b run
# leaked lookup_rule specifically, not just update_character.
LEAK_PATTERN = re.compile(
    r'update_character|lookup_rule|\{"name"\s*:\s*"(?:update_character|lookup_rule)"',
    re.IGNORECASE,
)


@dataclass
class TurnResult:
    index: int
    action: str
    expected: str
    expected_target: str | None
    called: bool
    called_targets: list[str]
    narration: str
    leaked_text: bool
    correct: bool | None  # None for ambiguous turns - not scored


async def run_scenario(narrator, max_history_messages: int | None = None) -> list[TurnResult]:
    session_kwargs = {"session_id": SESSION_ID}
    if max_history_messages is not None:
        session_kwargs["max_history_messages"] = max_history_messages
    session = Session(**session_kwargs)
    events: list[Envelope] = []

    async def broadcast(envelope: Envelope) -> None:
        events.append(envelope)

    async def send_to(recipient_id: str, envelope: Envelope) -> None:
        events.append(envelope)

    # store=None: this is a throwaway scenario run, never touches sessions/.
    # enable_opening_scene=False: the fixed SCENARIO below expects each of
    # its 8 turns to correspond to exactly one narrate() call in order - an
    # opening-scene call on join would shift that indexing. The opening
    # scene is a separate, not-yet-benchmarked feature (see ROADMAP.md item 6).
    engine = GameEngine(session, narrator, broadcast, send_to, store=None, enable_opening_scene=False)

    await engine.handle(
        Envelope(
            type="join_session",
            session_id=SESSION_ID,
            sender_id=PLAYER_ID,
            payload={"player_name": PLAYER_NAME},
        )
    )

    results: list[TurnResult] = []
    for i, turn in enumerate(SCENARIO, start=1):
        before = len(events)
        await engine.handle(
            Envelope(
                type="player_action",
                session_id=SESSION_ID,
                sender_id=PLAYER_ID,
                payload={"text": turn["action"]},
            )
        )
        new_events = events[before:]

        narration = "".join(
            e.payload.get("text", "")
            for e in new_events
            if e.type == "log_entry" and e.payload.get("kind") == "narration"
        )

        called_targets: list[str] = []
        for e in new_events:
            if e.type == "character_update":
                called_targets.append("self")
            elif e.type == "npc_update":
                called_targets.append(e.payload.get("name", "?"))

        called = bool(called_targets)
        leaked = bool(LEAK_PATTERN.search(narration))

        correct: bool | None
        if turn["expected"] == EXPECT_NO_CALL:
            correct = not called
        elif turn["expected"] == EXPECT_CALL:
            correct = called and turn["target"] in called_targets
        else:
            correct = None

        results.append(
            TurnResult(
                index=i,
                action=turn["action"],
                expected=turn["expected"],
                expected_target=turn["target"],
                called=called,
                called_targets=called_targets,
                narration=narration,
                leaked_text=leaked,
                correct=correct,
            )
        )

    return results


def print_report(
    results: list[TurnResult], model_label: str, backend: str, max_history_messages: int | None
) -> None:
    history_label = "default" if max_history_messages is None else f"{max_history_messages} messages"
    print(f"\n=== Live reliability check: {backend}/{model_label} (max_history={history_label}) ===\n")

    for r in results:
        tag = {True: "PASS", False: "FAIL", None: " ?  "}[r.correct]
        leak = "  [LEAKED TOOL-CALL TEXT]" if r.leaked_text else ""
        print(
            f"turn {r.index} [{tag}] expected={r.expected}({r.expected_target}) "
            f"actual_targets={r.called_targets}{leak}"
        )
        print(f"  action:    {r.action}")
        snippet = r.narration[:160] + ("..." if len(r.narration) > 160 else "")
        print(f"  narration: {snippet}\n")

    scored = [r for r in results if r.correct is not None]
    passed = [r for r in scored if r.correct]
    leaked_count = sum(1 for r in results if r.leaked_text)

    print("--- summary ---")
    print(f"scored turns: {len(scored)}/{len(results)} (ambiguous turns excluded from scoring)")
    if scored:
        print(f"correct: {len(passed)}/{len(scored)} ({100 * len(passed) / len(scored):.0f}%)")
    else:
        print("correct: n/a (no scored turns)")
    print(f"leaked pseudo-tool-call text: {leaked_count}/{len(results)} turns")


def write_json(
    results: list[TurnResult],
    model_label: str,
    backend: str,
    max_history_messages: int | None,
    out_path: Path,
) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
        "max_history_messages": max_history_messages,
        "turns": [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"wrote {out_path}")


def run_stats(results: list[TurnResult]) -> dict:
    """Single-run stats reused by both the per-run summary line in repeat
    mode and the aggregate across all repeats."""
    scored = [r for r in results if r.correct is not None]
    passed = [r for r in scored if r.correct]
    return {
        "scored": len(scored),
        "correct": len(passed),
        "rate": (len(passed) / len(scored)) if scored else None,
        "real_calls": sum(1 for r in results if r.called),
        "leaked": sum(1 for r in results if r.leaked_text),
        "total_turns": len(results),
    }


def aggregate_stats(all_results: list[list[TurnResult]]) -> dict:
    """Combines run_stats() across repeated runs of the identical scenario -
    see ROADMAP.md item 6's qwen3:8b entries for why this matters: a single
    run's result can look like a real signal and just be favorable sampling,
    something only visible once you have more than one run to compare."""
    per_run = [run_stats(results) for results in all_results]
    rates = [s["rate"] for s in per_run if s["rate"] is not None]
    total_scored = sum(s["scored"] for s in per_run)
    total_correct = sum(s["correct"] for s in per_run)
    return {
        "per_run": per_run,
        "mean_rate": (sum(rates) / len(rates)) if rates else None,
        "pooled_rate": (total_correct / total_scored) if total_scored else None,
        "total_scored": total_scored,
        "total_correct": total_correct,
    }


def print_repeat_report(
    all_results: list[list[TurnResult]], model_label: str, backend: str, max_history_messages: int | None
) -> None:
    history_label = "default" if max_history_messages is None else f"{max_history_messages} messages"
    print(
        f"\n=== Live reliability check: {backend}/{model_label} "
        f"(max_history={history_label}, {len(all_results)} repeats) ===\n"
    )

    agg = aggregate_stats(all_results)
    for i, (results, stats) in enumerate(zip(all_results, agg["per_run"]), start=1):
        rate_label = f"{100 * stats['rate']:.0f}%" if stats["rate"] is not None else "n/a"
        print(
            f"run {i}/{len(all_results)}: {stats['correct']}/{stats['scored']} correct ({rate_label}), "
            f"{stats['real_calls']}/{stats['total_turns']} real tool calls, "
            f"{stats['leaked']}/{stats['total_turns']} leaked"
        )

    print("\n--- aggregate ---")
    if agg["mean_rate"] is not None:
        print(f"mean of per-run rates: {100 * agg['mean_rate']:.0f}%")
        print(f"pooled: {agg['total_correct']}/{agg['total_scored']} ({100 * agg['pooled_rate']:.0f}%)")
        rates = [s["rate"] for s in agg["per_run"] if s["rate"] is not None]
        print(f"per-run spread: {', '.join(f'{100 * r:.0f}%' for r in rates)}")
    else:
        print("correct: n/a (no scored turns in any run)")


def write_json_repeat(
    all_results: list[list[TurnResult]],
    model_label: str,
    backend: str,
    max_history_messages: int | None,
    out_path: Path,
) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
        "max_history_messages": max_history_messages,
        "repeat": len(all_results),
        "aggregate": aggregate_stats(all_results),
        "runs": [[asdict(r) for r in results] for results in all_results],
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"wrote {out_path}")


async def main_async(args: argparse.Namespace) -> None:
    if args.backend == "ollama":
        from server.narrator_ollama import OllamaNarrator
        from server.rules import RulesIndex

        model_label = args.model or "qwen2.5:7b"
        narrator = OllamaNarrator(model=model_label, host=args.host, rules=RulesIndex.load_default())
    else:
        from server.narrator import AnthropicNarrator

        model_label = args.model or "claude-sonnet-5"
        narrator = AnthropicNarrator(model=model_label)

    if args.repeat == 1:
        results = await run_scenario(narrator, max_history_messages=args.max_history_messages)
        print_report(results, model_label, args.backend, args.max_history_messages)
        if args.out:
            write_json(results, model_label, args.backend, args.max_history_messages, Path(args.out))
        return

    all_results = []
    for i in range(args.repeat):
        print(f"running {i + 1}/{args.repeat}...", flush=True)
        all_results.append(await run_scenario(narrator, max_history_messages=args.max_history_messages))

    print_repeat_report(all_results, model_label, args.backend, args.max_history_messages)
    if args.out:
        write_json_repeat(all_results, model_label, args.backend, args.max_history_messages, Path(args.out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["ollama", "anthropic"], default="ollama")
    parser.add_argument("--model", default=None, help="Override the backend's default model.")
    parser.add_argument("--host", default=None, help="Ollama host URL, if not the default.")
    parser.add_argument("--out", default=None, help="Write a JSON report to this path for later diffing.")
    parser.add_argument(
        "--max-history-messages",
        type=int,
        default=None,
        help=(
            "Override Session.max_history_messages (default: the production default, 12 - "
            "6 turns). 2 messages = 1 turn of memory. Use 0 for no history at all. For "
            "quantifying the history-window tradeoff - see ROADMAP.md item 6."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run the scenario this many times against the same model and report an "
            "aggregate (mean/pooled rate, per-run spread) instead of one sample. A single "
            "run can look like a real signal and just be favorable sampling - see the "
            "qwen3:8b entries in ROADMAP.md item 6 for a real example. Each repeat costs "
            "as much time as one run, so this is expensive on CPU-only inference."
        ),
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
