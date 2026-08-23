from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable

import ollama

from .lore import WorldBible, load_default_world_bible
from .narrator import LOOKUP_RULE_TOOL, UPDATE_CHARACTER_TOOL, ApplyUpdate, RequestRoll, UpdateWorld
from .rules import RulesIndex
from .state import ABILITY_KEYS, SKILL_ABILITIES

OLLAMA_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Narrate outcomes vividly but concisely (3-5 sentences per turn). Track consequences
of the player's actions, introduce complications, and always end by implicitly or
explicitly inviting the player's next action, in open-ended prose — never as a
numbered or bulleted list of options to choose from. Never break character.

The acting character's ability scores, real modifiers, and AC are in their sheet
(character_summary's stats/stat_modifiers/ac) - let them inform how you narrate what
the character is good or bad at, and how easy or hard they are to hit, even though you
have no request_roll tool to apply them to mechanically.

You have two tools available:
- lookup_rule: use before improvising crunchy mechanics (monster stats, spell details,
  class features, equipment, conditions) so numbers stay consistent from turn to turn.
- update_character: call this whenever your narration describes something that should
  mechanically change the acting character OR a named NPC/monster — damage, healing,
  gaining or losing an item, or applying/clearing a condition. Narration alone doesn't
  change a sheet; this tool does. Omit target (or use 'self') for the acting character;
  pass an NPC's name as target to introduce or update its own tracked sheet, so its
  wounds and conditions persist turn to turn instead of being forgotten. Call it after
  you've decided the outcome, in the same turn you narrate it. When you introduce a new
  NPC worth remembering, give it a brief notes value too (a sentence on its personality,
  goal, or relationship to the party) — update that note later if the relationship
  changes, so a recurring character feels continuous instead of reset each time they
  appear. Set disposition too when it's clear (hostile/neutral/friendly) — a structured
  value to stay consistent against turn to turn, separate from the free-text notes. When
  the character/NPC rests for a meaningful stretch (camping overnight, resting after a
  fight), use the rest field ('short' or 'long') instead of guessing an hp_delta - the
  engine computes the real amount healed.

For anything not covered by lookup_rule, invent original content in the spirit of the
genre rather than claiming to search for real published material — you have no way to
search the web."""


def _to_ollama_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


OLLAMA_TOOLS = [_to_ollama_tool(LOOKUP_RULE_TOOL), _to_ollama_tool(UPDATE_CHARACTER_TOOL)]

MAX_TOOL_ROUNDS = 4

# The default path (see structured_output below), not merely an
# experiment anymore - a live, 5-repeat qwen2.5:7b comparison against the
# harness's own scenario found this roughly doubles real tool-call
# correctness over native tool-calling (66% vs 29% pooled), the first real
# improvement across six prior experiments in ROADMAP.md item 6's tool-call
# reliability investigation, all of which plateaued around 29% regardless
# of model, scale, or prompt changes. The mechanism: instead of asking the
# model to *decide* whether to invoke a separate tool (a repeatedly-missed
# step across qwen2.5/llama3.1/qwen3), the entire response is constrained
# to a JSON schema that always has a mechanical_change field to fill in -
# "should I call this?" becomes "fill in this field", a different problem
# shape small models handle more reliably than tool-selection, evidenced
# by real tool-call *attempts* jumping from 0-2/8 to 6-7/8 turns per run.
# Started as a minimal first slice (target, hp_delta, add_condition - just
# enough to validate the core hypothesis against the reliability harness's
# own scenario, which only ever needs those three) and has since grown
# rest/notes/disposition/cast_spell to close the update_character parity
# gap those first six fields left open - see ROADMAP.md. Still not full
# parity: no lookup_rule support (the harness's scoring never exercises
# it), and max_hp/add_item/magic_bonus/remove_item/remove_condition remain
# uncovered - a real, known gap, not silently accepted.
_OUTCOME_PROPERTIES = {
    "narration": {
        "type": "string",
        "description": "The narrated outcome, in character, 3-5 sentences, open-ended prose.",
    },
    "mechanical_change": {
        "type": "boolean",
        "description": "True if this turn's outcome changes anyone's HP, inventory, or conditions.",
    },
    "target": {
        "type": "string",
        "description": (
            "Who the mechanical change applies to - 'self' for the acting character, or "
            "an NPC's name (whoever actually got hurt or changed, not necessarily who "
            "acted). Only meaningful when mechanical_change is true."
        ),
    },
    "hp_delta": {
        "type": "integer",
        "description": "HP change - negative for damage, positive for healing. 0 if not applicable.",
    },
    "add_condition": {
        "type": "string",
        "description": "A condition to apply (e.g. 'poisoned'), or an empty string if none.",
    },
    "rest": {
        "type": "string",
        "enum": ["short", "long", ""],
        "description": (
            "Set when the character/NPC rests for a meaningful stretch of time (camping "
            "overnight, resting after a fight) instead of guessing hp_delta - the engine "
            "computes the real amount healed. 'long' fully restores HP; 'short' restores "
            "about half of what's missing. Empty string if not applicable. Don't combine "
            "with a non-zero hp_delta in the same response."
        ),
    },
    "notes": {
        "type": "string",
        "description": (
            "A brief standing note about this character/NPC (personality, goal, "
            "relationship to the party), replacing any previous note. Most useful on an "
            "NPC's introduction or when the relationship meaningfully changes. Empty "
            "string if not applicable."
        ),
    },
    "disposition": {
        "type": "string",
        "enum": ["hostile", "neutral", "friendly", ""],
        "description": (
            "An NPC/monster's current attitude toward the party. Set on introduction if "
            "clear from context, and update later if the relationship meaningfully "
            "changes. Not meaningful for 'self' - empty string if not applicable."
        ),
    },
    "cast_spell": {
        "type": "string",
        "description": (
            "Set only when the acting character casts one of their own known spells (e.g. "
            "'fire bolt', 'cure wounds') - the engine deducts the real spell slot "
            "automatically. Only meaningful for the acting character, never an NPC target. "
            "Empty string if no spell was cast."
        ),
    },
}

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": dict(_OUTCOME_PROPERTIES),
    "required": ["narration", "mechanical_change"],
}

# _OUTCOME_PROPERTIES minus `narration` - the missed-change follow-up check
# (OllamaNarrator.check_missed_change, below) reviews narration that
# already happened and streamed to the player; there's no new narration
# to write, so unlike STRUCTURED_OUTPUT_SCHEMA above, `narration` isn't
# even offered as a field, rather than requiring the model to fill in a
# throwaway value that would never be shown to anyone.
MISSED_CHANGE_SCHEMA = {
    "type": "object",
    "properties": {key: value for key, value in _OUTCOME_PROPERTIES.items() if key != "narration"},
    "required": ["mechanical_change"],
}

MISSED_CHANGE_SYSTEM_PROMPT = """You are the Dungeon Master reviewing your own narration from a moment
ago, given below, which did not call update_character. Respond with a single JSON object matching
the given schema. Set `mechanical_change` to true only if that narration describes a real change to
a character's or NPC's hp, inventory, or conditions that should have been recorded, and fill in
`target`/`hp_delta`/`add_condition` accordingly - target is whoever actually got hurt or changed,
not simply whoever acted. Set `rest` ('short' or 'long') if the narration described a meaningful
rest - the engine computes the real healing, so don't guess an hp_delta for it. Set `cast_spell`
if the acting character cast one of their own known spells. Set `notes`/`disposition` if an NPC
was introduced or a relationship meaningfully changed - these can be a real correction even when
`mechanical_change` is false. Otherwise set `mechanical_change` to false and leave every field at
its default."""


def _has_outcome_change(data: dict) -> bool:
    """The single apply/propose gate for any _OUTCOME_PROPERTIES-shaped
    response: mechanical_change OR any of the four fields that count as a
    real change on their own. Shared by _narrate_structured,
    check_missed_change, and propose_correction so all three keep the same
    semantics (the review paths originally gated on mechanical_change only
    and silently dropped notes-only/rest-only corrections)."""
    return bool(
        data.get("mechanical_change")
        or any(data.get(field) for field in ("rest", "notes", "disposition", "cast_spell"))
    )


def _outcome_update(data: dict) -> dict:
    """Builds the update_character-shaped dict from any _OUTCOME_PROPERTIES-
    shaped response - shared by _narrate_structured (a normal turn),
    check_missed_change (which applies it), and propose_correction (which
    hands it back for the player to confirm)."""
    update = {"target": data.get("target") or "self"}
    if data.get("hp_delta"):
        update["hp_delta"] = data["hp_delta"]
    if data.get("add_condition"):
        update["add_condition"] = data["add_condition"]
    if data.get("rest"):
        update["rest"] = data["rest"]
    if data.get("notes"):
        update["notes"] = data["notes"]
    if data.get("disposition"):
        update["disposition"] = data["disposition"]
    if data.get("cast_spell"):
        update["cast_spell"] = data["cast_spell"]
    return update


PROPOSE_CORRECTION_SYSTEM_PROMPT = """You are the Dungeon Master reviewing your own narration from a moment
ago, given below. You have already decided no correction is certain enough to auto-apply. But if the
player confirms that something mechanically changed anyway, the sheet needs a concrete record. Respond
with a single JSON object matching the given schema. Set `mechanical_change` to true only if the
narration plausibly describes a real change to a character's or NPC's hp, inventory, or conditions,
and fill in `target`/`hp_delta`/`add_condition` with your best guess of what the update_character call
would have been - target is whoever actually got hurt or changed, not simply whoever acted. Also set
`rest` ('short' or 'long') if the narration plausibly described a meaningful rest, `cast_spell` if the
acting character plausibly cast one of their own known spells, and `notes`/`disposition` if an NPC was
plausibly introduced or a relationship meaningfully changed - these can be a real proposal even when
`mechanical_change` is false. Otherwise set `mechanical_change` to false and leave every field at its
default."""

# Extends _OUTCOME_PROPERTIES with a second, independent decision: does this
# turn need a real dice roll before the outcome can even be narrated? (See
# _narrate_structured's two-pass docstring for the full reasoning.) A model
# response with roll_requested=true still fills in narration/
# mechanical_change (the schema requires them either way, and Ollama's
# format constraint doesn't cleanly express "field X only when field Y is
# true" for a small local model) - but those fields are provisional and
# discarded in that case, since they'd have been written without knowing
# the real roll outcome yet. Kept minimal, the same "just enough to test
# the hypothesis" scope _OUTCOME_PROPERTIES itself already established for
# update_character - dice notation isn't part of this at all, since the
# engine's own request_roll closure already defaults to "1d20" and adds
# the real ability/proficiency modifiers itself once skill/ability is named.
#
# Opt-in only (OLLAMA_ROLL_REQUESTS, off by default) - unlike structured
# output itself, this has NOT been validated at the same rigor (a real
# 5-repeat harness study). Three live spot-check runs against qwen2.5:7b
# (5 turns each, this session, 2026-08-09) found real signal but real
# inconsistency: 6/6 obviously-certain actions correctly triggered no roll
# (no false positives across any run), but only 7/9 genuinely-uncertain
# ones triggered one - and with real run-to-run variance (3/3, then 1/3,
# then 3/3), the same "doesn't reproduce" pattern ROADMAP.md's own
# investigation already documented for other tool-call decisions. Field
# completeness when a roll did fire was weaker still: only 1 of those 7
# had a fully correct skill+ability+DC - most left skill/ability blank
# (resolving as an unmodified d20 server-side, not a real stat-backed
# roll) or picked a semantically wrong skill (lock-picking tagged
# "deception" instead of "sleight_of_hand"). See ROADMAP.md for the full
# numbers - kept off by default until a proper --repeat study either
# confirms this holds up or finds it doesn't, the same bar every other
# reliability claim in this file was held to.
STRUCTURED_OUTPUT_ROLL_SCHEMA = {
    "type": "object",
    "properties": {
        **_OUTCOME_PROPERTIES,
        "roll_requested": {
            "type": "boolean",
            "description": (
                "True only if this action's outcome is genuinely uncertain and needs a real "
                "dice roll before it can be narrated - an attack, a skill check, a saving "
                "throw. False for anything with an obvious, certain outcome. When true, "
                "narration/mechanical_change above are ignored - the real narration comes "
                "from a follow-up once the roll is known, so just leave them at reasonable "
                "placeholders."
            ),
        },
        "roll_skill": {
            "type": "string",
            "enum": sorted(SKILL_ABILITIES),
            "description": (
                "Only when roll_requested is true and this is a skill check: the real 5e "
                "skill name (e.g. 'stealth', 'perception'). Omit for a roll that isn't a "
                "skill check."
            ),
        },
        "roll_ability": {
            "type": "string",
            "enum": list(ABILITY_KEYS),
            "description": (
                "Only when roll_requested is true: the ability score behind this roll (a "
                "raw ability check, a saving throw, most attacks). Omit if roll_skill "
                "already names a skill - its governing ability applies automatically."
            ),
        },
        "roll_dc": {
            "type": "integer",
            "description": (
                "Only when roll_requested is true: the difficulty class the roll must meet "
                "or beat. Omit for a roll with no pass/fail threshold."
            ),
        },
        "roll_kind": {
            "type": "string",
            "enum": ["attack", "save", "check"],
            "description": "Only when roll_requested is true: what kind of roll this is.",
        },
    },
    "required": ["narration", "mechanical_change", "roll_requested"],
}

# The follow-up call's schema, once a requested roll's real result is known
# - same outcome shape as pass one, just without the roll-deciding fields
# (a roll already happened; this call only narrates and applies its
# consequences).
# v2 scene facts (docs/protocol.md "Protocol v2 additions - Scene envelope"):
# structured fields the DM decides alongside mechanics; the engine turns them
# into a scene_update broadcast. Decided, never parsed out of prose.
SCENE_PROPERTIES = {
    "npcs_present": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Names of NPCs/monsters present in this scene right now.",
    },
    "points_of_interest": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Interactable things in the scene worth examining.",
    },
    "suggested_actions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Up to 4 concrete things the acting player might do next.",
    },
}


def _with_scene_fields(schema: dict) -> dict:
    return {**schema, "properties": {**schema["properties"], **SCENE_PROPERTIES}}


def _strip_narration(schema: dict) -> dict:
    """The two-phase decide call decides everything EXCEPT prose - forced-JSON
    narration measurably flattens it, so narration is written in a separate
    unconstrained call (docs/REBUILD_PLAN.md two-phase turn)."""
    props = {k: v for k, v in schema["properties"].items() if k != "narration"}
    required = [r for r in schema.get("required", []) if r != "narration"]
    return {**schema, "properties": props, "required": required}



STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": dict(_OUTCOME_PROPERTIES),
    "required": ["narration", "mechanical_change"],
}


DECIDE_SCHEMA = _strip_narration(STRUCTURED_OUTPUT_ROLL_SCHEMA)
DECIDE_FOLLOWUP_SCHEMA = _strip_narration(STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA)

# A second, independent extension alongside the roll fields above - unlike
# a roll, a world-state change (location, a new/completed objective, a
# newly-discovered place) has no ordering problem: it's simply a
# consequence of the turn's outcome, decided at the same time as
# mechanical_change, in whichever call ends up producing the final
# narration (the only call, if no roll fires; the follow-up, if one does).
# So this doesn't need its own two-pass mechanism - just extra properties
# merged onto whichever schema is already in play, via _with_world_fields
# below. Deliberately a small slice of the real update_world tool
# (server/narrator.py's UPDATE_WORLD_TOOL): location/add_objective/
# complete_objective/add_location only - no summary (harder for a small
# model to write well without duplicating narration), no expire_objective/
# fail_objective/remove_objective/set_flag/clear_flag/connect_locations
# (connect_locations especially - a 2-element array is more failure-prone
# for constrained JSON than a plain string field). add_location doubles as
# the fix for a real, separately-reported gap: the client's Map tab
# (client/app.py's CharacterSheetPanel) only ever populates from
# add_location/connect_locations, and this tool's Anthropic-only history
# meant it never had - see ROADMAP.md for the live numbers this shipped
# opt-in (not default) on the strength of.
_WORLD_PROPERTIES = {
    "world_change": {
        "type": "boolean",
        "description": (
            "True if this turn's outcome changes the campaign's persistent world state - "
            "the party's location, an objective's status, or a newly-discovered place worth "
            "remembering. False for anything that's just passing scene detail."
        ),
    },
    "location": {
        "type": "string",
        "description": "The party's new current location, only if it just changed. Leave blank otherwise.",
    },
    "mood": {
        "type": "string",
        "description": (
            "The current scene's mood/environment tag - a short descriptor of the present "
            "moment's emotional tone, e.g. 'tense', 'foreboding', 'hopeful', 'festive'. "
            "Set it when the tone of the current scene meaningfully shifts, not every turn. "
            "Leave blank if unchanged."
        ),
    },
    "add_objective": {
        "type": "string",
        "description": (
            "A new active objective/plot thread/quest hook worth tracking for the rest of "
            "the campaign, in plain language. Leave blank if none."
        ),
    },
    "complete_objective": {
        "type": "string",
        "description": (
            "The exact text of an existing active objective this turn's outcome completed. "
            "Leave blank if none."
        ),
    },
    "add_location": {
        "type": "string",
        "description": (
            "Name of a new place worth remembering on the map, if one was just discovered "
            "(even before the party has left it). Leave blank if none."
        ),
    },
}

# Revised 2026-08-10 (ROADMAP.md's update_world reliability investigation)
# after the original wording above measured at 0/12 real recall (0%, not
# the 33% a single earlier one-off run had suggested) across 3 full
# --repeat runs (scripts/live_world_reliability_check.py) against
# qwen2.5:7b - the model wrote narration that unambiguously described a
# location change or a real quest hook, but consistently answered
# world_change: false anyway. The old wording buried the instruction as a
# same-priority afterthought ("Also decide...") tacked onto the end of the
# base prompt, competing with mechanical_change for attention with no
# concrete example of what should trigger it.
#
# This version was arrived at empirically, not by first guess - two
# earlier candidates were tried and rejected on real evidence, not
# intuition:
#   - A version giving location an explicit "always true on arrival"
#     anchor, plus a blanket "any task-like dialogue is always an
#     objective" instruction and a "when unsure, prefer true" bias: fixed
#     location cleanly (0% -> ~100% recall on arrival/travel turns,
#     reproduced across 6 repeat runs) but introduced a NEW false
#     positive - the neutral "just making conversation" turn started
#     inventing a spurious third objective out of ambient rumor dialogue,
#     every single run.
#   - A version narrowing the objective trigger to a direct, explicit
#     request: fixed the false positive (neutral turns correctly stayed
#     unchanged again) but then missed even a genuinely explicit, direct
#     quest request 3/3 times - net zero versus the original wording for
#     objectives specifically, not an improvement.
#
# The version below keeps only what measured as a clean, reproducible win
# with no observed regression: the explicit "always true" anchor for
# location specifically (verified 3/3 correct across every repeat run
# tested, in an isolated worktree, not assumed to generalize from one
# sample). Objective add/complete keep more conservative wording than the
# rejected "always"/"prefer true" versions, matching what real testing
# showed didn't help without also hurting - completing an objective by
# its own exact prior text in particular never worked in any variant
# tried (0% in every configuration), a genuine, still-open reliability
# gap this rewrite does not claim to have solved.
#
# complete_objective follow-up (2026-08-10): the leading hypothesis was
# that 0% recall was a *recall* problem - asking a small model to retype
# an objective's exact text correctly from several turns back in the
# rolling history window. NarratorBackend.narrate() gained a
# world_summary parameter (WorldState.narrator_context(), server/
# state.py) specifically to test this - giving the DM the real, current
# active objectives directly in the same turn's own prompt, so
# complete_objective could copy the text rather than recall it. **This
# hypothesis was wrong.** Re-measured across 10 repeat runs with the
# exact objective text sitting directly in the prompt: complete_objective
# still fired successfully 0/10 times. Verified with a raw-response
# diagnostic, not just the aggregate number: on a turn whose narration
# unambiguously resolved the one active objective explicitly listed in
# that same call's own "World state" section, the model still answered
# world_change: false. The real bottleneck isn't recalling the text - the
# model doesn't reliably recognize "this narration resolves an active
# goal" as a world_change-worthy event category at all, a different and
# apparently deeper problem than the location-tracking gap this same
# investigation did manage to fix. world_summary is kept anyway (grounds
# location/add_objective in real current state rather than nothing,
# measured as not worse than the prompt-only version - 24/50 vs 21/40
# pooled, well within this scenario's own already-documented 40-80%
# per-run noise band) - but it is not a fix for complete_objective, and
# isn't claimed to be one.
WORLD_UPDATE_PROMPT_ADDENDUM = """

You must also track world_change, exactly as carefully as mechanical_change above - it is not
optional or secondary. Set world_change to true whenever ANY of these happen this turn, and
fill in the matching field:
- The party arrives somewhere, travels somewhere, or is now clearly in a different place than
  before -> set `location` to that place's name. This includes a first arrival into any named
  location - always true then.
- An NPC directly and explicitly asks you for help, or asks you to find, rescue, retrieve, or
  deliver something specific -> set `add_objective` to a short plain-language version of that
  exact request. Vague rumors, gossip, or background lore with no direct request attached are
  NOT an objective - leave add_objective blank for those.
- Your own narration this turn describes successfully finishing a task an NPC earlier and
  directly asked of you -> set `complete_objective` to that task's exact text. If a "World
  state" section above lists current active objectives, copy the matching one's text from
  there character-for-character - don't retype it from memory.
- A new place worth remembering is discovered -> set `add_location` to its name.
- The emotional tone of the current scene meaningfully shifts (entering a haunted ruin, the mood
  lifting after a victory) -> set `mood` to a short descriptor of the new tone ('tense',
  'foreboding', 'hopeful', 'festive'). Do not set it every turn - only when the tone really
  changes, so it stays a stable, useful tag rather than a running commentary.
Set world_change to false for anything else - idle conversation, background rumors with no
direct request, examining something without traveling, or combat with no location/goal change."""


def _with_world_fields(schema: dict, include_world: bool) -> dict:
    if not include_world:
        return schema
    return {
        "type": "object",
        "properties": {**schema["properties"], **_WORLD_PROPERTIES},
        "required": [*schema["required"], "world_change"],
    }


def _with_world_prompt(prompt: str, include_world: bool) -> str:
    return prompt + WORLD_UPDATE_PROMPT_ADDENDUM if include_world else prompt


STRUCTURED_OUTPUT_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
Respond with a single JSON object matching the given schema - never prose outside that JSON,
never a tool call. `narration` is your in-character response (3-5 sentences, open-ended prose,
never a numbered or bulleted list of options). Set `mechanical_change` to true whenever the
narration describes something that should change a sheet - damage, healing, gaining or losing
an item, or a new/cleared condition - and fill in `target`/`hp_delta`/`add_condition`
accordingly. `target` is whoever actually got hurt or changed, not simply whoever acted - if
the acting character attacks someone else and that other creature takes the damage, target is
that NPC's name, never the acting character's own name or 'self'. Set mechanical_change to
false (and leave the other fields at their defaults) for a turn with no real mechanical
outcome. When the character/NPC rests for a meaningful stretch (camping overnight, resting
after a fight), set `rest` to 'short' or 'long' instead of guessing an hp_delta - the engine
computes the real amount healed; don't combine rest with a non-zero hp_delta. When the acting
character casts one of their own known spells, set `cast_spell` to its name - the engine
deducts the real spell slot; still fill in hp_delta/add_condition separately for the spell's
actual effect. When you introduce a new NPC worth remembering, or a recurring one's
relationship to the party meaningfully changes, set `notes` (a sentence on personality/goal/
relationship) and `disposition` (hostile/neutral/friendly) - these two can be the only real
change on a turn, independent of mechanical_change. Leave rest/notes/disposition/cast_spell as
empty strings when not applicable. Never break character in `narration`."""

# Opt-in (few_shot_example, off by default - see OllamaNarrator.__init__),
# a single worked example appended once to the base structured-output
# prompt above. Distinct from the per-turn reminder tried and reverted in
# ROADMAP.md item 6's fifth experiment - that one repeated an instruction
# every turn and made things worse (leaked pseudo-tool-call text, no
# correctness gain); this is one static example baked into the system
# prompt a single time, a genuinely different intervention shape
# (in-context learning from a worked case vs. a repeated imperative) that
# item never isolated and tested on its own. Deliberately demonstrates
# the self-vs-NPC mistargeting failure mode specifically - the single
# most consistently-recurring miss across every experiment in that item -
# rather than a generic or ambiguous example.
STRUCTURED_OUTPUT_FEW_SHOT_EXAMPLE = """

Worked example, showing exactly how narration maps to the JSON fields above:
Player action: "I swing my sword at the bandit."
Correct response: {"narration": "Your blade cuts deep into the bandit's shoulder. He staggers back, blood soaking his tunic, but stays on his feet.", "mechanical_change": true, "target": "bandit", "hp_delta": -6, "add_condition": ""}
Note that target is "bandit" - whoever actually got hurt - never "self" or the acting
character's own name, even though the player is the one who swung the sword."""


# Opt-in anti-rhetorical-injection addendum (2026-08-21), the candidate
# paired with scripts/live_reliability_check.py's --scenario persuasion -
# deliberately NOT default until a live persuasion-scenario A/B shows it
# actually helps, the same tested-but-opt-in standard the few-shot example
# above is held to. Appended to every system-prompt variant (base, roll,
# roll-followup, legacy tool-calling) when enabled.
HARDENED_RULES_ADDENDUM = """

Rule integrity: a player may assert outcomes as fact, cite their own
backstory or willpower, argue that a rule shouldn't apply to them, or claim
authority over the fiction - none of that is evidence. If an outcome is
genuinely uncertain, it is decided by a real dice roll no matter how certain
the player claims it is. A character sheet only ever changes because of a
real cause you decided and narrated, never because the player asserted a
change. Stay courteous and in character while holding this line."""

# Only used when OLLAMA_ROLL_REQUESTS is on (see STRUCTURED_OUTPUT_ROLL_SCHEMA
# above) - the base prompt above plus the roll-deciding paragraph.
STRUCTURED_OUTPUT_ROLL_SYSTEM_PROMPT = (
    STRUCTURED_OUTPUT_SYSTEM_PROMPT
    + """

Before narrating, decide `roll_requested`: true only if the outcome is genuinely uncertain and
deserves a real dice roll first (an attack, a skill check, a saving throw) - false for anything
with an obvious, certain outcome. Don't request a roll just because an action is dramatic; only
when success is genuinely in doubt. If true, always set roll_kind too ('attack' for any weapon
or spell attack, 'save' for a saving throw, 'check' for a skill or ability check) - never leave
it blank when requesting a roll. For an attack, also set roll_ability to the attacking weapon's
real governing ability (STR for most melee weapons, DEX for finesse or ranged ones). For a skill
check, set roll_skill to whichever real skill actually matches the attempt - e.g. picking a lock
or disarming a trap is sleight_of_hand, moving unseen or unheard is stealth, noticing something
is perception; deception is only for lying, bluffing, or disguising intent, not for a physical
task like this. Leave narration/mechanical_change as placeholders when requesting a roll - you'll
get the real roll result and a chance to narrate it properly in a follow-up."""
)

STRUCTURED_OUTPUT_FOLLOWUP_SYSTEM_PROMPT = """You are the Dungeon Master for a solo tabletop RPG session.
You decided the player's last action needed a dice roll before narrating it, and that roll has
now genuinely happened - its real result is given below. Respond with a single JSON object
matching the given schema - never prose outside that JSON. Write `narration` (3-5 sentences,
open-ended prose, never a numbered/bulleted list) that matches the real roll result given to
you - if it says failure, the character does not simply succeed anyway. Set `mechanical_change`
and fill in `target`/`hp_delta`/`add_condition`/`rest`/`notes`/`disposition`/`cast_spell` the
same way a normal turn would, now informed by whether the roll actually succeeded. Never break
character."""

# Two-phase decide variants (docs/REBUILD_PLAN.md): derived from the prompts
# above by stripping every narration instruction - DECIDE_SCHEMA /
# DECIDE_FOLLOWUP_SCHEMA have no narration field (prose is written in the
# separate unconstrained call), and a prompt that orders the model to "Write
# `narration`" next to a schema without one measurably degrades the fields
# that DO exist on small models (first post-v2 harness run: 2/7 vs the 66%
# single-call baseline; re-run with these fixed prompts: 4/7 underscore-folded,
# see ROADMAP.md item 32).
DECIDE_SYSTEM_PROMPT = (
    STRUCTURED_OUTPUT_SYSTEM_PROMPT
    .replace(
        "Respond with a single JSON object matching the given schema - never prose outside that JSON,\n"
        "never a tool call. `narration` is your in-character response (3-5 sentences, open-ended prose,\n"
        "never a numbered or bulleted list of options). ",
        "Decide the outcome of this turn. Respond with a single JSON object matching the given schema -\n"
        "never prose outside that JSON, never a tool call. Do NOT write narration; your structured\n"
        "decisions are narrated in a separate step. ",
    )
    .replace(" Never break character in `narration`.", "")
)
DECIDE_FOLLOWUP_SYSTEM_PROMPT = (
    STRUCTURED_OUTPUT_FOLLOWUP_SYSTEM_PROMPT
    .replace(
        "Respond with a single JSON object\nmatching the given schema - never prose outside that JSON. Write `narration` (3-5 sentences,\nopen-ended prose, never a numbered/bulleted list) that matches the real roll result given to\nyou - if it says failure, the character does not simply succeed anyway.",
        "Respond with a single JSON object matching the given schema - never prose outside that JSON.\nDo NOT write narration; your structured decisions are narrated in a separate step. Decide the\noutcome to match the real roll result given to you - if it says failure, the character does not\nsimply succeed anyway.",
    )
    .replace(" Never break\ncharacter.", "")
)

# Guard: the replaces above must actually fire - if the source text drifts and
# one silently no-ops, the decide prompt would order narration-writing against
# a narration-free schema again (the exact 2/7 r1 failure).
for _p in (DECIDE_SYSTEM_PROMPT, DECIDE_FOLLOWUP_SYSTEM_PROMPT):
    assert "Do NOT write narration" in _p, "decide-prompt replace() no-op'd"


class OllamaNarrator:
    """Local NarratorBackend backed by Ollama. No web_search — that's an
    Anthropic-hosted server tool with no local equivalent, so this backend
    leans on lookup_rule and the model's own judgment instead."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str | None = None,
        rules: RulesIndex | None = None,
        structured_output: bool = True,
        two_phase: bool = True,
        roll_requests: bool = False,
        world_updates: bool = False,
        world_bible: WorldBible | None = None,
        few_shot_example: bool = False,
        hardened_rules: bool = False,
    ):
        self._client = ollama.AsyncClient(host=host)
        self._model = model
        self._rules = rules or RulesIndex.load_default()
        # Computed once, appended to every system prompt variant below -
        # present on every narrate() call regardless of the rolling
        # history window's size, so the world's own facts (server/lore's
        # WorldBible) can't scroll out of context and drift over a long
        # session. Same content Anthropic's narrator.py appends to its own
        # system prompt.
        # The hardened-rules addendum rides every prompt variant - roll
        # turns are exactly where rhetorical pressure lands, so the base
        # prompt alone wouldn't cover the failure mode.
        hardened = HARDENED_RULES_ADDENDUM if hardened_rules else ""
        lore_block = (world_bible or load_default_world_bible()).system_prompt_block()
        suffix = lore_block + hardened
        self._tool_calling_system_prompt = OLLAMA_SYSTEM_PROMPT + suffix

        structured_prompt = STRUCTURED_OUTPUT_SYSTEM_PROMPT
        if few_shot_example:
            structured_prompt += STRUCTURED_OUTPUT_FEW_SHOT_EXAMPLE
        self._structured_system_prompt = structured_prompt + suffix
        self._structured_roll_system_prompt = STRUCTURED_OUTPUT_ROLL_SYSTEM_PROMPT + suffix
        self._structured_followup_system_prompt = STRUCTURED_OUTPUT_FOLLOWUP_SYSTEM_PROMPT + suffix
        # Two-phase variants - narration-free system prompts matching the
        # narration-free DECIDE schemas (see the constants' own comment).
        self._decide_system_prompt = DECIDE_SYSTEM_PROMPT + suffix
        self._decide_followup_system_prompt = DECIDE_FOLLOWUP_SYSTEM_PROMPT + suffix
        # Defaults on (see STRUCTURED_OUTPUT_SCHEMA above for why) - a
        # real constructor flag rather than a separate class, since every
        # other piece of state (client/model/rules) is identical either
        # way and this project already has precedent (max_history_messages,
        # scripts/live_reliability_check.py's --repeat) for exposing a
        # real behavioral knob as a plain parameter rather than a subclass.
        # False (or OLLAMA_STRUCTURED_OUTPUT=0, create_ollama_narrator's
        # own env var) is a real escape hatch to the legacy path, not a
        # dead option - a session missing rest/notes/disposition/
        # cast_spell might prefer full update_character parity over the
        # higher correctness rate on the fields structured mode does cover.
        self._structured_output = structured_output
        # Two-phase turns (docs/REBUILD_PLAN.md): a schema-constrained decide
        # call makes every structured decision (roll, sheet deltas, world
        # deltas, scene facts) and a separate UNCONSTRAINED streaming call
        # writes the prose - constrained JSON is reliability, but constraining
        # narration measurably flattens it, so prose never lives in the
        # schema on this path. Default ON; OLLAMA_TWO_PHASE=0 is the escape
        # hatch back to the single-call structured path (kept intact for
        # A/B measurement, this project's standard practice).
        self._two_phase = two_phase
        # Scene facts ride the two-phase decide schema; the single-call path
        # keeps its exact shape (and the legacy tool-calling path never had
        # them). Engine reads this via getattr before passing scene_sink.
        self.supports_scene_facts = structured_output and two_phase
        # Defaults OFF, unlike structured_output above - see
        # STRUCTURED_OUTPUT_ROLL_SCHEMA's own docstring for why: real
        # signal from live spot-checks, but not yet validated at the same
        # rigor (a proper --repeat study) that earned structured_output its
        # default-on status. Ignored entirely when structured_output is
        # False - the legacy tool-calling path has never supported
        # request_roll and this doesn't change that.
        self._roll_requests = roll_requests
        # Defaults OFF, same reasoning as roll_requests above - not yet
        # validated at the rigor structured_output's own default earned.
        # Independent of roll_requests (see _WORLD_PROPERTIES' own
        # docstring for why a world-state change has no two-pass ordering
        # problem the way a roll does) - either can be on without the
        # other. Also ignored entirely when structured_output is False.
        self._world_updates = world_updates

    def narrate(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
        world_summary: str | None = None,
        scene_sink: Callable[[dict], None] | None = None,
    ) -> AsyncIterator[str]:
        if self._structured_output:
            if self._two_phase:
                return self._narrate_two_phase(
                    history, character_summary, action_text, apply_update, request_roll, update_world, world_summary, scene_sink
                )
            return self._narrate_structured(
                history, character_summary, action_text, apply_update, request_roll, update_world, world_summary
            )
        return self._narrate_tool_calling(history, character_summary, action_text, apply_update)

    async def summarize(self, prior_summary: str, turns: list[dict]) -> str:
        prior = f"Summary so far:\n{prior_summary}\n\n" if prior_summary else ""
        from .narrator import _turns_to_text

        response = await self._client.chat(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You maintain a running campaign summary for a tabletop RPG "
                        "session. Compress the recent events into a durable recap: "
                        "who the characters and recurring NPCs are, what happened, "
                        "what was promised or owed, and every unresolved thread. Keep "
                        "proper names exactly. At most ~180 words. Output only the "
                        "summary text."
                    ),
                },
                {"role": "user", "content": f"{prior}Recent turns:\n{_turns_to_text(turns)}"},
            ],
            stream=False,
        )
        return (response.message.content or "").strip()

    async def check_missed_change(
        self, narration: str, character_summary: str, apply_update: ApplyUpdate
    ) -> bool:
        """See NarratorBackend.check_missed_change (server/narrator.py) for
        the full "why". Structured-output only - the legacy native tool-
        calling path has no equivalent, matching request_roll/world_updates'
        own existing "ignored entirely when structured_output is False"
        precedent (see __init__ above). Reuses MISSED_CHANGE_SCHEMA (a
        narration-less variant of STRUCTURED_OUTPUT_SCHEMA) via one
        constrained, non-streamed call - the same mechanism a normal turn
        already uses, just with a correction-focused prompt and no
        narration field to write."""
        if not self._structured_output:
            return False
        prompt = f"Character:\n{character_summary}\n\nYour narration:\n{narration}"
        messages = [
            {"role": "system", "content": MISSED_CHANGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = await self._client.chat(
            model=self._model, messages=messages, format=MISSED_CHANGE_SCHEMA, stream=False
        )
        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            return False
        if not _has_outcome_change(data):
            return False
        apply_update(_outcome_update(data))
        return True

    async def propose_correction(self, narration: str, character_summary: str) -> dict | None:
        """See NarratorBackend.propose_correction (server/narrator.py) for
        the full "why". Same mechanism as check_missed_change - one
        constrained, non-streamed MISSED_CHANGE_SCHEMA call - but framed as
        a hypothesis ("if something did change, what would it have been")
        rather than a decision, and it returns the proposed update instead
        of applying it, for the player to confirm via /apply. Structured-
        output only, matching check_missed_change's own opt-out."""
        if not self._structured_output:
            return None
        prompt = f"Character:\n{character_summary}\n\nYour narration:\n{narration}"
        messages = [
            {"role": "system", "content": PROPOSE_CORRECTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = await self._client.chat(
            model=self._model, messages=messages, format=MISSED_CHANGE_SCHEMA, stream=False
        )
        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            return None
        if not _has_outcome_change(data):
            return None
        return _outcome_update(data)

    async def _narrate_structured(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
        world_summary: str | None = None,
    ) -> AsyncIterator[str]:
        """The structured-output path (see STRUCTURED_OUTPUT_SCHEMA above) -
        constrains the entire response to JSON via Ollama's format parameter
        instead of native tool-calling. Not streamed: constrained generation
        doesn't produce meaningfully parseable partial JSON chunk-by-chunk
        the way free-form tool-calling text does, so this yields narration
        as one chunk once a complete response is in hand -
        `_narrate_and_apply`'s own buffering (`buffer += chunk`) handles a
        single big chunk exactly the same as many small ones.

        Two model calls, not one, whenever roll_requested comes back true -
        deliberately, not an oversight. request_roll's real dice result
        can't be known until after the roll happens, so a single JSON
        response can't both decide a roll is needed *and* write narration
        that's guaranteed to match its outcome (a fabricated "you succeed"
        sitting next to a roll that, moments later, actually came back a
        failure). The first call only decides whether/how to roll; the
        engine rolls for real; a second call writes the actual narration
        already knowing that real result - the same two-step shape Claude's
        native request_roll tool-call already gets for free from a real
        multi-turn tool round trip, just done as two constrained JSON calls
        instead. Costs real extra latency, but only on turns the model
        itself judges genuinely uncertain - most turns still cost exactly
        one call, unchanged from before this existed."""
        schema = STRUCTURED_OUTPUT_ROLL_SCHEMA if self._roll_requests else STRUCTURED_OUTPUT_SCHEMA
        system_prompt = self._structured_roll_system_prompt if self._roll_requests else self._structured_system_prompt
        # world fields are additive on top of whichever schema/prompt was
        # just selected above - this call might BE the final one (no roll
        # requested) or might not (a follow-up call replaces it below), but
        # either way it needs to be able to express a world change if this
        # turns out to be the response that matters.
        schema = _with_world_fields(schema, self._world_updates)
        system_prompt = _with_world_prompt(system_prompt, self._world_updates)
        prompt = f"Character:\n{character_summary}\n\n"
        # Only when world_updates is actually on - otherwise world_summary
        # would describe fields the schema doesn't even expose this call,
        # pure noise. Given directly rather than left for the model to
        # infer/recall from history alone - see NarratorBackend.narrate's
        # own docstring (server/narrator.py) for why this exists at all:
        # complete_objective needs an exact prior text match, and recalling
        # that correctly from several turns back measured at 0% (ROADMAP.md).
        # world_summary now also carries the tracked-NPC roster
        # (_npc_roster, server/engine.py) - grounded context the DM should
        # see regardless of whether update_world tracking is on, and an
        # empty summary still means no section at all, so a session with
        # world updates off behaves exactly as before unless NPCs exist.
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        prompt += f"Player action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]
        response = await self._client.chat(model=self._model, messages=messages, format=schema, stream=False)

        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            # A real, possible failure mode for constrained generation on a
            # small model - malformed JSON despite the schema constraint.
            # Surfaces the raw content as narration rather than silently
            # losing the turn, the same "don't hide a real failure" spirit
            # _on_player_action's own exception handling already has.
            yield response.message.content or ""
            return

        if self._roll_requests and data.get("roll_requested") and request_roll is not None:
            roll_update: dict = {}
            if data.get("roll_skill"):
                roll_update["skill"] = data["roll_skill"]
            if data.get("roll_ability"):
                roll_update["ability"] = data["roll_ability"]
            if data.get("roll_dc") is not None:
                roll_update["dc"] = data["roll_dc"]
            if data.get("roll_kind"):
                roll_update["roll_kind"] = data["roll_kind"]
            roll_result_text = request_roll(roll_update)

            followup_prompt = f"Character:\n{character_summary}\n\n"
            if world_summary:
                followup_prompt += f"World state:\n{world_summary}\n\n"
            followup_prompt += f"Player action: {action_text}\n\nRoll result: {roll_result_text}"
            followup_schema = _with_world_fields(STRUCTURED_OUTPUT_FOLLOWUP_SCHEMA, self._world_updates)
            followup_system_prompt = _with_world_prompt(self._structured_followup_system_prompt, self._world_updates)
            followup_messages: list[dict] = [
                {"role": "system", "content": followup_system_prompt},
                *history,
                {"role": "user", "content": followup_prompt},
            ]
            response = await self._client.chat(
                model=self._model, messages=followup_messages, format=followup_schema, stream=False
            )
            try:
                data = json.loads(response.message.content or "")
            except json.JSONDecodeError:
                yield response.message.content or ""
                return

        yield data.get("narration", "")

        # rest/notes/disposition/cast_spell can each be the only real change
        # on a turn (an NPC introduction with just a note, a rest with no
        # separate hp_delta) - mechanical_change's own schema description
        # only promises "HP, inventory, or conditions", so gating on it
        # alone would silently drop those. Checked independently, same
        # falsy-omission style as hp_delta/add_condition above.
        if _has_outcome_change(data):
            apply_update(_outcome_update(data))

        if self._world_updates and data.get("world_change") and update_world is not None:
            world_update: dict = {}
            if data.get("location"):
                world_update["location"] = data["location"]
            if data.get("mood"):
                world_update["mood"] = data["mood"]
            if data.get("add_objective"):
                world_update["add_objective"] = data["add_objective"]
            if data.get("complete_objective"):
                world_update["complete_objective"] = data["complete_objective"]
            if data.get("add_location"):
                world_update["add_location"] = data["add_location"]
            if world_update:
                update_world(world_update)

    async def _narrate_two_phase(
        self,
        history: list[dict],
        character_summary: str,
        action_text: str,
        apply_update: ApplyUpdate,
        request_roll: RequestRoll | None = None,
        update_world: UpdateWorld | None = None,
        world_summary: str | None = None,
        scene_sink: Callable[[dict], None] | None = None,
    ) -> AsyncIterator[str]:
        """Two-phase turn (see the _two_phase constructor note). Phase 1: a
        narration-free decide call under DECIDE_SCHEMA (+world/scene fields);
        when it requests a roll, the engine rolls for real and a follow-up
        decide call re-decides knowing the result - the same ordering
        discipline _narrate_structured already documents. Structured changes
        land BEFORE prose streams so sheet/world updates resolve as the
        narration describing them starts. Phase 2: unconstrained streaming
        prose told what was decided, so it cannot contradict the record."""
        prompt = f"Character:\n{character_summary}\n\n"
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        prompt += f"Player action: {action_text}"
        base_messages: list[dict] = [
            {"role": "system", "content": self._decide_system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]

        schema = _with_scene_fields(_with_world_fields(DECIDE_SCHEMA, self._world_updates))

        response = await self._client.chat(model=self._model, messages=base_messages, format=schema, stream=False)
        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            yield response.message.content or ""
            return

        if request_roll is not None and data.get("roll_requested"):
            roll_update: dict = {}
            if data.get("roll_skill"):
                roll_update["skill"] = data["roll_skill"]
            if data.get("roll_ability"):
                roll_update["ability"] = data["roll_ability"]
            if data.get("roll_dc") is not None:
                roll_update["dc"] = data["roll_dc"]
            if data.get("roll_kind"):
                roll_update["roll_kind"] = data["roll_kind"]
            roll_result_text = request_roll(roll_update)
            followup_schema = _with_scene_fields(_with_world_fields(DECIDE_FOLLOWUP_SCHEMA, self._world_updates))
            followup_messages = [
                {"role": "system", "content": self._decide_followup_system_prompt},
                *history,
                {"role": "user", "content": f"{prompt}\n\nReal dice result: {roll_result_text}\nDecide the outcome now."},
            ]
            response = await self._client.chat(model=self._model, messages=followup_messages, format=followup_schema, stream=False)
            try:
                data = json.loads(response.message.content or "")
            except json.JSONDecodeError:
                yield response.message.content or ""
                return

        if data.get("mechanical_change") or any(data.get(field) for field in ("rest", "notes", "disposition", "cast_spell")):
            apply_update(_outcome_update(data))
        if self._world_updates and data.get("world_change") and update_world is not None:
            world_delta: dict = {}
            for field in ("location", "mood", "add_objective", "complete_objective", "add_location"):
                if data.get(field):
                    world_delta[field] = data[field]
            if world_delta:
                update_world(world_delta)
        if scene_sink is not None:
            facts = {key: data.get(key) or [] for key in ("npcs_present", "points_of_interest", "suggested_actions")}
            facts["suggested_actions"] = facts["suggested_actions"][:4]
            scene_sink(facts)

        decided = {k: v for k, v in data.items() if k not in SCENE_PROPERTIES}
        narrate_messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "You are the Dungeon Master. Narrate the outcome in vivid, "
                    "concise open-ended prose (3-5 sentences). Never break "
                    "character. Output prose only."
                ),
            },
            *history,
            {
                "role": "user",
                "content": f"{prompt}\n\nDecided outcome (narrate exactly this): {json.dumps(decided, ensure_ascii=False)}",
            },
        ]
        stream = await self._client.chat(model=self._model, messages=narrate_messages, stream=True)
        async for chunk in stream:
            if chunk.message.content:
                yield chunk.message.content

    async def _narrate_tool_calling(
        self, history: list[dict], character_summary: str, action_text: str, apply_update: ApplyUpdate
    ) -> AsyncIterator[str]:
        # request_roll/update_world aren't parameters here (unlike the outer
        # narrate() this is dispatched from, which keeps them for
        # NarratorBackend interface parity) - neither is in OLLAMA_TOOLS, so
        # local models can't call them yet. See ROADMAP.md item 6 - this
        # session's investigation found small local models already miss the
        # one existing tool on most clearly-warranted turns; adding more
        # required tool calls before narration would only compound that.
        # Scoped to AnthropicNarrator first; local support is a deliberate
        # follow-up, not an oversight.
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": self._tool_calling_system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            stream = await self._client.chat(
                model=self._model,
                messages=messages,
                tools=OLLAMA_TOOLS,
                stream=True,
            )

            tool_calls = []
            async for chunk in stream:
                if chunk.message.content:
                    yield chunk.message.content
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)

            if not tool_calls:
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                if tc.function.name == "lookup_rule":
                    result = self._rules.lookup(
                        tc.function.arguments.get("category", ""),
                        tc.function.arguments.get("name", ""),
                    )
                elif tc.function.name == "update_character":
                    result = apply_update(dict(tc.function.arguments))
                else:
                    continue
                messages.append({"role": "tool", "content": result})


def create_ollama_narrator() -> OllamaNarrator:
    # OLLAMA_STRUCTURED_OUTPUT defaults on, matching OllamaNarrator's own
    # default - a real escape hatch to the legacy native-tool-calling path
    # (set to "0"/"false"/"no"), not required for normal use. See
    # STRUCTURED_OUTPUT_SCHEMA's own docstring and ROADMAP.md for why this
    # is the default: a live, 5-repeat qwen2.5:7b comparison found it
    # roughly doubles real tool-call correctness (66% vs 29% pooled) over
    # native tool-calling, the first real improvement across six
    # experiments in this project's own tool-call reliability investigation.
    structured = os.environ.get("OLLAMA_STRUCTURED_OUTPUT", "true").strip().lower() not in ("0", "false", "no")
    # OLLAMA_ROLL_REQUESTS defaults OFF, unlike OLLAMA_STRUCTURED_OUTPUT
    # above - see STRUCTURED_OUTPUT_ROLL_SCHEMA's own docstring for why:
    # real signal, not yet validated at the same rigor. Opt in with "1"/
    # "true"/"yes".
    roll_requests = os.environ.get("OLLAMA_ROLL_REQUESTS", "false").strip().lower() in ("1", "true", "yes")
    # OLLAMA_WORLD_UPDATES defaults OFF, same reasoning/opt-in convention as
    # OLLAMA_ROLL_REQUESTS above - see _WORLD_PROPERTIES' own docstring.
    world_updates = os.environ.get("OLLAMA_WORLD_UPDATES", "false").strip().lower() in ("1", "true", "yes")
    # OLLAMA_TWO_PHASE defaults ON with structured output - the two-phase
    # decide->narrate split is docs/REBUILD_PLAN.md's headline narrator
    # change; "0" escapes to the single-call path for A/B measurement.
    two_phase = os.environ.get("OLLAMA_TWO_PHASE", "true").strip().lower() not in ("0", "false", "no")
    return OllamaNarrator(
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        host=os.environ.get("OLLAMA_HOST"),
        structured_output=structured,
        two_phase=two_phase,
        roll_requests=roll_requests,
        world_updates=world_updates,
    )
