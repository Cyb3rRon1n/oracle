"""Live tool-call reliability check for a NarratorBackend.

Runs a fixed scenario through the real GameEngine against a real, live
backend (Ollama or Anthropic) - not a mock - and reports whether
`update_character` actually fired when narration clearly warranted it,
whether it targeted the right sheet (acting character vs. named NPC), and
whether the model leaked pseudo-tool-call text into narration instead of
using the real tool-call channel.

Background: ROADMAP.md items 4-6. This replaces the one-off scratchpad
scripts used during that investigation with a reusable, version-controlled
tool so the same check can be re-run consistently across models, backends,
and candidate fixes.

Two scenarios, selected via --scenario, never mixed together:
- 'combat' (default, SCENARIO below) is a faithful reconstruction of the
  shape described in ROADMAP.md item 4's "second pass" (two sequential
  bandits, a mix of call/no-call/ambiguous turns targeting hp_delta/
  add_condition) - the exact original action strings were only paraphrased
  there, not preserved verbatim, so this is not a byte-exact replay of that
  session. Every historical pooled/mean-rate number in ROADMAP.md item 6 was
  measured against this scenario's fixed 8 turns.
- 'field-parity' (FIELD_PARITY_SCENARIO below, added 2026-08-20) is a
  separate 6-turn scenario exercising rest/notes/disposition/cast_spell -
  the fields structured output's schema didn't cover until that entry. Uses
  a real 'cleric' character (not combat's blank class), since cast_spell
  needs known spells/spell slots to mean anything.

Usage:
    python -m scripts.live_reliability_check --backend ollama --model qwen2.5:7b
    python -m scripts.live_reliability_check --backend anthropic --out results.json
    python -m scripts.live_reliability_check --model qwen3:8b --repeat 3  # aggregate over 3 runs
    python -m scripts.live_reliability_check --tool-calling  # reproduce the legacy pre-structured-output baseline
    python -m scripts.live_reliability_check --scenario field-parity --repeat 5  # rest/notes/disposition/cast_spell
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
# The four field-parity expectation types (ROADMAP.md's rest/notes/
# disposition/cast_spell schema-parity entry, 2026-08-20) - each checks a
# real, observable sheet change rather than just "did a call happen",
# since these fields can be the only real change on a turn (see
# _narrate_structured's broadened trigger in server/narrator_ollama.py).
EXPECT_REST = "rest"
EXPECT_CAST_SPELL = "cast_spell"
EXPECT_NOTE = "note"
EXPECT_DISPOSITION = "disposition"

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

# A second, additive scenario (2026-08-20) - never mixed into SCENARIO above,
# so every historical pooled/mean-rate number this file's ROADMAP.md entries
# cite (all computed against SCENARIO's fixed 8 turns) stays exactly
# comparable to a future combat-scenario run. rest/cast_spell need real
# pre-conditions SCENARIO's blank-class Torvin doesn't have - a spellcasting
# class for known_spells/spell_slots (see FIELD_PARITY_CHARACTER_CLASS
# below), and a genuine self-damage turn first, since a "long rest" at
# already-full HP is a real no-op in CharacterSheet.apply_update() (server/
# state.py) with nothing observable to score either way.
FIELD_PARITY_CHARACTER_CLASS = "cleric"

FIELD_PARITY_SCENARIO = [
    {
        "action": "A trap springs as I step through the doorway, a dart grazing my arm.",
        "expected": EXPECT_CALL,
        "target": "self",
    },
    {
        "action": "I bandage the wound and rest by the fire for the night.",
        "expected": EXPECT_REST,
        "target": "self",
    },
    {
        "action": "I lay a hand on my own wound and murmur a prayer, calling on cure wounds.",
        "expected": EXPECT_CAST_SPELL,
        "target": "self",
    },
    {
        "action": "A frightened pilgrim we cross paths with shares a story about the family she's trying to reach.",
        "expected": EXPECT_NOTE,
        "target": "pilgrim",
    },
    {
        "action": "A masked figure lunges from the shadows, blade drawn, clearly hostile.",
        "expected": EXPECT_DISPOSITION,
        "target": "figure",
        "expected_disposition": "hostile",
    },
    {
        "action": "I take a moment to look up at the night sky before moving on.",
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


async def run_scenario(
    narrator,
    max_history_messages: int | None = None,
    scenario: list[dict] = SCENARIO,
    character_class: str | None = None,
) -> list[TurnResult]:
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

    join_payload = {"player_name": PLAYER_NAME}
    if character_class:
        join_payload["character_class"] = character_class
    await engine.handle(
        Envelope(
            type="join_session",
            session_id=SESSION_ID,
            sender_id=PLAYER_ID,
            payload=join_payload,
        )
    )

    results: list[TurnResult] = []
    for i, turn in enumerate(scenario, start=1):
        # Snapshotted straight from the real session, not derived from
        # broadcast events - rest/cast_spell fields have no dedicated
        # envelope of their own, so the only reliable signal is the real
        # sheet's own hp/spell_slots before vs. after this turn.
        player = session.characters[PLAYER_ID]
        before_hp = player.hp
        before_max_hp = player.max_hp
        before_spell_slots = sum(player.spell_slots.values())

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
        expected = turn["expected"]
        if expected == EXPECT_NO_CALL:
            correct = not called
        elif expected == EXPECT_CALL:
            # Case-insensitive, matching the real production engine's own
            # NPC matching (server/engine.py's apply_update closure keys
            # NPCs by target.casefold()) - a real scoring bug found while
            # analyzing a structured-output experiment's results, not
            # introduced by it: this harness's own exact-match comparison
            # was scoring "Bandit" as a miss against a scenario expecting
            # "bandit", something the real engine has never actually
            # treated as wrong. Affects every prior experiment this
            # harness has ever scored, not just this one - see ROADMAP.md.
            correct = called and turn["target"].casefold() in {t.casefold() for t in called_targets}
        elif expected == EXPECT_REST:
            # A "long rest" at already-full HP is a real no-op in
            # CharacterSheet.apply_update() (server/state.py) - nothing
            # observable either way, so this can't distinguish "the model
            # didn't set rest" from "it did, but there was nothing to
            # heal" (e.g. an earlier turn's own EXPECT_CALL missed).
            # Ambiguous rather than penalized, same as EXPECT_AMBIGUOUS.
            after_hp = session.characters[PLAYER_ID].hp
            correct = None if before_hp >= before_max_hp else after_hp > before_hp
        elif expected == EXPECT_CAST_SPELL:
            # cast_spell only ever deducts a real slot (server/engine.py's
            # _cast_spell) - a decrease is the one unambiguous signal a
            # real cast happened, regardless of which spell/slot level.
            after_spell_slots = sum(session.characters[PLAYER_ID].spell_slots.values())
            correct = None if before_spell_slots <= 0 else after_spell_slots < before_spell_slots
        elif expected == EXPECT_NOTE:
            npc = session.npcs.get(turn["target"].casefold())
            correct = npc is not None and bool(npc.notes)
        elif expected == EXPECT_DISPOSITION:
            npc = session.npcs.get(turn["target"].casefold())
            correct = npc is not None and npc.disposition == turn.get("expected_disposition")
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
    results: list[TurnResult],
    model_label: str,
    backend: str,
    max_history_messages: int | None,
    scenario_name: str = "combat",
) -> None:
    history_label = "default" if max_history_messages is None else f"{max_history_messages} messages"
    print(
        f"\n=== Live reliability check: {backend}/{model_label} "
        f"(scenario={scenario_name}, max_history={history_label}) ===\n"
    )

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
    scenario_name: str = "combat",
) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
        "max_history_messages": max_history_messages,
        "scenario": scenario_name,
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
    all_results: list[list[TurnResult]],
    model_label: str,
    backend: str,
    max_history_messages: int | None,
    scenario_name: str = "combat",
) -> None:
    history_label = "default" if max_history_messages is None else f"{max_history_messages} messages"
    print(
        f"\n=== Live reliability check: {backend}/{model_label} "
        f"(scenario={scenario_name}, max_history={history_label}, {len(all_results)} repeats) ===\n"
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
    scenario_name: str = "combat",
) -> None:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model_label,
        "max_history_messages": max_history_messages,
        "scenario": scenario_name,
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
            structured_output=not args.tool_calling, few_shot_example=args.few_shot,
        )
    else:
        from server.narrator import AnthropicNarrator

        model_label = args.model or "claude-sonnet-5"
        narrator = AnthropicNarrator(model=model_label)

    scenario = FIELD_PARITY_SCENARIO if args.scenario == "field-parity" else SCENARIO
    character_class = FIELD_PARITY_CHARACTER_CLASS if args.scenario == "field-parity" else None

    if args.repeat == 1:
        results = await run_scenario(
            narrator, max_history_messages=args.max_history_messages, scenario=scenario, character_class=character_class
        )
        print_report(results, model_label, args.backend, args.max_history_messages, args.scenario)
        if args.out:
            write_json(results, model_label, args.backend, args.max_history_messages, Path(args.out), args.scenario)
        return

    all_results = []
    for i in range(args.repeat):
        print(f"running {i + 1}/{args.repeat}...", flush=True)
        all_results.append(
            await run_scenario(
                narrator,
                max_history_messages=args.max_history_messages,
                scenario=scenario,
                character_class=character_class,
            )
        )

    print_repeat_report(all_results, model_label, args.backend, args.max_history_messages, args.scenario)
    if args.out:
        write_json_repeat(
            all_results, model_label, args.backend, args.max_history_messages, Path(args.out), args.scenario
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["ollama", "anthropic"], default="ollama")
    parser.add_argument("--model", default=None, help="Override the backend's default model.")
    parser.add_argument("--host", default=None, help="Ollama host URL, if not the default.")
    parser.add_argument("--out", default=None, help="Write a JSON report to this path for later diffing.")
    parser.add_argument(
        "--scenario",
        choices=["combat", "field-parity"],
        default="combat",
        help=(
            "'combat' (default) is the original 8-turn target/hp_delta/add_condition scenario "
            "every historical number in ROADMAP.md item 6 was measured against. 'field-parity' "
            "is a separate, additive 6-turn scenario (a real 'cleric' character, not the "
            "combat scenario's blank class) exercising rest/notes/disposition/cast_spell - "
            "the fields the 2026-08-20 structured-output schema-parity entry added. Never "
            "mixed together, so combat's own historical numbers stay comparable."
        ),
    )
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
    parser.add_argument(
        "--tool-calling",
        action="store_true",
        help=(
            "Ollama backend only - use the legacy native tool-calling path instead of the "
            "default structured-output one (server/narrator_ollama.py's OllamaNarrator "
            "structured_output=True is the default for both this harness and "
            "create_ollama_narrator() - a live 5-repeat comparison found it roughly doubles "
            "real tool-call correctness, see ROADMAP.md item 6). Pass this to reproduce the "
            "older baseline numbers documented earlier in that item. Ignored for --backend "
            "anthropic."
        ),
    )
    parser.add_argument(
        "--few-shot",
        action="store_true",
        help=(
            "Ollama backend, structured output only - append a single worked example to the "
            "system prompt once (server/narrator_ollama.py's STRUCTURED_OUTPUT_FEW_SHOT_EXAMPLE), "
            "distinct from the per-turn reminder tried and reverted in ROADMAP.md item 6's fifth "
            "experiment. Off by default - a real, untested candidate, not yet validated the way "
            "structured output itself was."
        ),
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
