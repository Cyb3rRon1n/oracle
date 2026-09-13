"""Live update_world reliability check for a NarratorBackend.

Runs a fixed 5-turn scenario (arrive somewhere -> an NPC hands over a real
quest hook -> a neutral turn -> travel to the quest location -> resolve it)
through the real GameEngine against a real, live backend - not a mock - and
reports whether WorldState actually changed the way the turn's narration
warranted (a new location, a new objective, an unrelated neutral turn
correctly triggering no change, a location arrived at, an objective
completed).

Background: ROADMAP.md's `OLLAMA_WORLD_UPDATES` entry found 33% recall
(4/12) from a single live run - a real number, but never turned into a
reusable, --repeat-capable harness the way update_character's own
reliability investigation (scripts/live_reliability_check.py) already has.
This closes that gap so world-update reliability (and any future candidate
fix to it) can be measured the same rigorous way, not re-derived by hand
each time - see that item's own "use --repeat N before trusting any
single-run result" lesson, learned the hard way from qwen3:8b's one
non-reproducing "promising" run.

Usage:
    python -m scripts.live_world_reliability_check --model qwen2.5:7b
    python -m scripts.live_world_reliability_check --repeat 3 --out results.json
    python -m scripts.live_world_reliability_check --backend anthropic
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from server.engine import GameEngine
from server.state import Session
from shared.protocol import Envelope

SESSION_ID = "world-reliability-check"
PLAYER_ID = "player-1"
PLAYER_NAME = "Rowan"

EXPECT_LOCATION = "location"
EXPECT_OBJECTIVE_ADD = "objective_add"
EXPECT_OBJECTIVE_COMPLETE = "objective_complete"
EXPECT_NO_CHANGE = "no_change"

# The exact shape ROADMAP.md's original OLLAMA_WORLD_UPDATES entry
# described in prose ("arrive in town -> an NPC hands over a real quest
# hook -> a neutral turn -> travel to the quest location -> resolve it") -
# reconstructed here as a real, reusable scenario so that finding's 33%
# number has something repeatable to compare against, not just a one-off
# note. Deliberately no combat/HP turns at all - this scenario isolates
# world-state tracking specifically, the way live_reliability_check.py's
# own scenario isolates update_character.
SCENARIO = [
    {
        "action": "I arrive at the edge of a small village called Millbrook and look around for anyone who might need a hand.",
        "expected": EXPECT_LOCATION,
    },
    {
        "action": "A worried villager rushes up to me, pleading for help finding her goat before nightfall - it wandered off toward the old mill.",
        "expected": EXPECT_OBJECTIVE_ADD,
    },
    {
        "action": "While I'm here, I ask around town for any other news, just making conversation.",
        "expected": EXPECT_NO_CHANGE,
    },
    {
        "action": "I head out of Millbrook toward the old mill where the goat was last seen.",
        "expected": EXPECT_LOCATION,
    },
    {
        "action": "I find the goat tangled in some brush near the mill, free it, and lead it back toward Millbrook.",
        "expected": EXPECT_OBJECTIVE_COMPLETE,
    },
]

# ROADMAP.md's "long-session memory frequency" open question: update_world's
# tool-call side is measured (SCENARIO above, 100% on qwen3:8b), but that's a
# short 5-turn arc where nearly every turn demands a change. The open
# question is different - whether the DM keeps WorldState.summary fresh over
# a genuinely long session with plenty of neutral chatter in between, not
# just at the handful of turns SCENARIO forces a change on. 22 turns: two
# full quest arcs (goat, then a bandit-camp rescue) plus a third hook teased
# at the end, deliberately interleaved with neutral turns (shopping, resting,
# idle chat) the same way a real session would be - no combat/HP turns, this
# isolates world/summary tracking specifically, same as SCENARIO does.
SCENARIO_LONG = [
    {"action": "I arrive at the edge of a small village called Millbrook and look around for anyone who might need a hand.", "expected": EXPECT_LOCATION},
    {"action": "A worried villager rushes up to me, pleading for help finding her goat before nightfall - it wandered off toward the old mill.", "expected": EXPECT_OBJECTIVE_ADD},
    {"action": "While I'm here, I ask around town for any other news, just making conversation.", "expected": EXPECT_NO_CHANGE},
    {"action": "I stop by the market stall to see what's for sale before heading out.", "expected": EXPECT_NO_CHANGE},
    {"action": "I head out of Millbrook toward the old mill where the goat was last seen.", "expected": EXPECT_LOCATION},
    {"action": "The mill is mostly collapsed. Picking through the rubble, I notice fresh boot prints heading further into the woods.", "expected": EXPECT_NO_CHANGE},
    {"action": "I find the goat tangled in some brush near the mill, free it, and lead it back toward Millbrook.", "expected": EXPECT_OBJECTIVE_COMPLETE},
    {"action": "Back in Millbrook, I hand the goat back to the relieved villager and thank her for the directions.", "expected": EXPECT_NO_CHANGE},
    {"action": "I take a room at the inn for the night and rest up.", "expected": EXPECT_NO_CHANGE},
    {"action": "Over breakfast, the innkeeper mentions bandits have been raiding travelers on the north road and one of the missing travelers is still unaccounted for.", "expected": EXPECT_OBJECTIVE_ADD},
    {"action": "I ask the innkeeper for anything else useful before I go looking for the bandit camp.", "expected": EXPECT_NO_CHANGE},
    {"action": "I follow the north road out of Millbrook, watching for any sign of the bandits.", "expected": EXPECT_LOCATION},
    {"action": "I spot a thin trail of smoke off the road and follow it to a rough camp, staying low in the brush to scout it first.", "expected": EXPECT_NO_CHANGE},
    {"action": "I count the guards and wait for a gap in their patrol before creeping closer.", "expected": EXPECT_NO_CHANGE},
    {"action": "Behind a tent I find the missing traveler tied up and gagged.", "expected": EXPECT_NO_CHANGE},
    {"action": "I cut the traveler's bindings and help them up, keeping my voice to a whisper.", "expected": EXPECT_OBJECTIVE_COMPLETE},
    {"action": "We slip out of the camp together and head back toward Millbrook before the bandits notice we're gone.", "expected": EXPECT_LOCATION},
    {"action": "Back in town, I bring the rescued traveler to the innkeeper, who thanks me and pours us both a drink.", "expected": EXPECT_NO_CHANGE},
    {"action": "I spend the evening at the tavern, trading stories with the locals.", "expected": EXPECT_NO_CHANGE},
    {"action": "An old traveler in the corner mentions an abandoned watchtower to the east that's supposedly haunted, and that no one who's gone up there has come back the same.", "expected": EXPECT_OBJECTIVE_ADD},
    {"action": "I ask around a bit more about the watchtower before turning in for the night.", "expected": EXPECT_NO_CHANGE},
    {"action": "In the morning, I restock some supplies at the market before setting out.", "expected": EXPECT_NO_CHANGE},
]


@dataclass
class WorldTurnResult:
    index: int
    action: str
    expected: str
    narration: str
    location_before: str
    location_after: str
    objectives_before: list[str]  # active objective texts before this turn
    objectives_after: list[str]
    completed_this_turn: list[str]
    world_update_fired: bool
    correct: bool
    summary_before: str
    summary_after: str
    summary_changed: bool


def _active_objective_texts(world) -> list[str]:
    return [o.text for o in world.objectives if o.status == "active"]


async def run_scenario(narrator, scenario: list[dict] = SCENARIO) -> list[WorldTurnResult]:
    session = Session(session_id=SESSION_ID)
    events: list[Envelope] = []

    async def broadcast(envelope: Envelope) -> None:
        events.append(envelope)

    async def send_to(recipient_id: str, envelope: Envelope) -> None:
        events.append(envelope)

    # store=None: throwaway scenario run, never touches sessions/.
    # enable_opening_scene=False: this scenario's own first turn is the
    # arrival - an opening-scene call on join would set world state (or at
    # least consume a narrate() call) before the scenario's own first
    # scored turn even runs, the same reason live_reliability_check.py's
    # scenario disables it too.
    engine = GameEngine(session, narrator, broadcast, send_to, store=None, enable_opening_scene=False)

    await engine.handle(
        Envelope(type="join_session", session_id=SESSION_ID, sender_id=PLAYER_ID, payload={"player_name": PLAYER_NAME})
    )

    results: list[WorldTurnResult] = []
    for i, turn in enumerate(scenario, start=1):
        location_before = session.world.location
        objectives_before = _active_objective_texts(session.world)
        completed_before = {o.text for o in session.world.objectives if o.status == "completed"}
        summary_before = session.world.summary

        before_events = len(events)
        await engine.handle(
            Envelope(type="player_action", session_id=SESSION_ID, sender_id=PLAYER_ID, payload={"text": turn["action"]})
        )
        new_events = events[before_events:]

        narration = "".join(
            e.payload.get("text", "") for e in new_events if e.type == "log_entry" and e.payload.get("kind") == "narration"
        )
        world_update_fired = any(e.type == "world_update" for e in new_events)

        location_after = session.world.location
        objectives_after = _active_objective_texts(session.world)
        completed_after = {o.text for o in session.world.objectives if o.status == "completed"}
        completed_this_turn = sorted(completed_after - completed_before)
        summary_after = session.world.summary
        summary_changed = summary_after != summary_before

        expected = turn["expected"]
        if expected == EXPECT_LOCATION:
            correct = location_after != location_before
        elif expected == EXPECT_OBJECTIVE_ADD:
            correct = len(objectives_after) > len(objectives_before)
        elif expected == EXPECT_OBJECTIVE_COMPLETE:
            correct = len(completed_this_turn) > 0
        else:  # EXPECT_NO_CHANGE
            correct = (
                location_after == location_before
                and objectives_after == objectives_before
                and not completed_this_turn
            )

        results.append(
            WorldTurnResult(
                index=i,
                action=turn["action"],
                expected=expected,
                narration=narration,
                location_before=location_before,
                location_after=location_after,
                objectives_before=objectives_before,
                objectives_after=objectives_after,
                completed_this_turn=completed_this_turn,
                world_update_fired=world_update_fired,
                correct=correct,
                summary_before=summary_before,
                summary_after=summary_after,
                summary_changed=summary_changed,
            )
        )

    return results


def run_stats(results: list[WorldTurnResult]) -> dict:
    correct = sum(1 for r in results if r.correct)
    return {
        "scored": len(results),
        "correct": correct,
        "rate": correct / len(results) if results else None,
        "world_updates_fired": sum(1 for r in results if r.world_update_fired),
    }


def summary_cadence_stats(results: list[WorldTurnResult]) -> dict:
    """How often WorldState.summary actually changed across a session - the
    metric for ROADMAP.md's "long-session memory frequency" question, distinct
    from run_stats' per-turn correctness (summary is freeform text, there's no
    ground truth to score it against, only whether it's kept moving)."""
    update_turns = [r.index for r in results if r.summary_changed]
    gaps: list[int] = []
    prev = 0
    for turn in update_turns:
        gaps.append(turn - prev)
        prev = turn
    gaps.append(len(results) - prev)  # trailing gap since the last update (or from turn 0 if never)
    return {
        "turns": len(results),
        "updates": len(update_turns),
        "update_turns": update_turns,
        "ever_set": any(r.summary_after for r in results),
        "longest_gap": max(gaps) if gaps else len(results),
        "final_summary": results[-1].summary_after if results else "",
    }


def aggregate_stats(all_results: list[list[WorldTurnResult]]) -> dict:
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


def print_report(results: list[WorldTurnResult], model_label: str, backend: str) -> None:
    print(f"\n=== Live update_world reliability check: {backend}/{model_label} ===\n")
    for r in results:
        tag = "PASS" if r.correct else "FAIL"
        print(f"turn {r.index} [{tag}] expected={r.expected} world_update_fired={r.world_update_fired}")
        print(f"  action:     {r.action}")
        print(f"  location:   {r.location_before!r} -> {r.location_after!r}")
        print(f"  objectives: {r.objectives_before} -> {r.objectives_after}  completed_this_turn={r.completed_this_turn}")
        snippet = r.narration[:160] + ("..." if len(r.narration) > 160 else "")
        print(f"  narration:  {snippet}")
        if r.summary_changed:
            preview = r.summary_after[:120] + ("..." if len(r.summary_after) > 120 else "")
            print(f"  summary ->  {preview}")
        print()

    stats = run_stats(results)
    print("--- summary ---")
    rate_label = f"{100 * stats['rate']:.0f}%" if stats["rate"] is not None else "n/a"
    print(f"correct: {stats['correct']}/{stats['scored']} ({rate_label})")
    print(f"world_update envelopes fired: {stats['world_updates_fired']}/{len(results)} turns")

    cadence = summary_cadence_stats(results)
    print("\n--- world.summary cadence ---")
    print(f"ever set: {cadence['ever_set']}")
    print(f"updated on turns: {cadence['update_turns'] or 'never'}")
    print(f"longest gap between updates (turns, includes start/end): {cadence['longest_gap']}")
    if cadence["final_summary"]:
        print(f"final summary: {cadence['final_summary']}")


def print_repeat_report(all_results: list[list[WorldTurnResult]], model_label: str, backend: str) -> None:
    print(f"\n=== Live update_world reliability check: {backend}/{model_label} ({len(all_results)} repeats) ===\n")
    agg = aggregate_stats(all_results)
    for i, (results, stats) in enumerate(zip(all_results, agg["per_run"]), start=1):
        rate_label = f"{100 * stats['rate']:.0f}%" if stats["rate"] is not None else "n/a"
        print(f"run {i}/{len(all_results)}: {stats['correct']}/{stats['scored']} correct ({rate_label})")

    print("\n--- aggregate ---")
    if agg["mean_rate"] is not None:
        print(f"mean of per-run rates: {100 * agg['mean_rate']:.0f}%")
        print(f"pooled: {agg['total_correct']}/{agg['total_scored']} ({100 * agg['pooled_rate']:.0f}%)")
        rates = [s["rate"] for s in agg["per_run"] if s["rate"] is not None]
        print(f"per-run spread: {', '.join(f'{100 * r:.0f}%' for r in rates)}")


def write_json(results: list[WorldTurnResult], model_label: str, backend: str, out_path: Path) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
        "turns": [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"wrote {out_path}")


def write_json_repeat(all_results: list[list[WorldTurnResult]], model_label: str, backend: str, out_path: Path) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
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
        narrator = OllamaNarrator(
            model=model_label, host=args.host, rules=RulesIndex.load_default(),
            structured_output=True, world_updates=True,
        )
    else:
        from server.narrator import AnthropicNarrator

        model_label = args.model or "claude-sonnet-5"
        narrator = AnthropicNarrator(model=model_label)

    scenario = SCENARIO_LONG if args.long else SCENARIO

    if args.repeat == 1:
        results = await run_scenario(narrator, scenario)
        print_report(results, model_label, args.backend)
        if args.out:
            write_json(results, model_label, args.backend, Path(args.out))
        return

    all_results = []
    for i in range(args.repeat):
        print(f"running {i + 1}/{args.repeat}...", flush=True)
        all_results.append(await run_scenario(narrator, scenario))

    print_repeat_report(all_results, model_label, args.backend)
    if args.out:
        write_json_repeat(all_results, model_label, args.backend, Path(args.out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["ollama", "anthropic"], default="ollama")
    parser.add_argument("--model", default=None, help="Override the backend's default model.")
    parser.add_argument("--host", default=None, help="Ollama host URL, if not the default.")
    parser.add_argument("--out", default=None, help="Write a JSON report to this path for later diffing.")
    parser.add_argument(
        "--repeat", type=int, default=1,
        help="Run the scenario this many times and report an aggregate - see live_reliability_check.py's own flag for why this matters.",
    )
    parser.add_argument(
        "--long", action="store_true",
        help="Use the 22-turn SCENARIO_LONG (two quest arcs plus neutral turns) instead of the "
        "5-turn SCENARIO, to measure WorldState.summary update cadence over a long session "
        "(ROADMAP.md's 'long-session memory frequency' question) rather than short-scenario recall.",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
