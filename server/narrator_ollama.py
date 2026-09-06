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

character_summary also carries the character's personality, ideals, bonds, and flaws.
Play to them - let them colour NPC reactions and complications - but never quote them
back at the player. When a player leans hard into one, reward it with Inspiration
(update_character, inspiration: true) - sparingly.

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

# The default structured-output path. Constraining the whole response to a
# schema with a mechanical_change field ("fill in this field") measured ~2x
# more reliable on local models than asking them to decide to call a tool
# ("should I call this?") - 66% vs 29% pooled, qwen2.5:7b, --repeat 5. See
# CHANGELOG's structured-output finding.
# Deliberately not full update_character parity: no lookup_rule, and
# max_hp/add_item/magic_bonus/remove_item/remove_condition are uncovered.
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

# _OUTCOME_PROPERTIES minus `narration`: the missed-change follow-up reviews
# narration that already streamed, so there's no new prose to write.
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
    """Apply/propose gate: mechanical_change, or any of rest/notes/disposition/
    cast_spell which each count as a real change on their own. Shared so
    _narrate_structured, check_missed_change and propose_correction agree."""
    return bool(
        data.get("mechanical_change")
        or any(data.get(field) for field in ("rest", "notes", "disposition", "cast_spell"))
    )


def _outcome_update(data: dict) -> dict:
    """Build the update_character-shaped dict from an _OUTCOME_PROPERTIES
    response - shared by the normal turn and both review paths."""
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

# _OUTCOME_PROPERTIES plus a second decision: does this turn need a dice roll
# before the outcome can be narrated? roll_requested=true still fills
# narration/mechanical_change (the schema requires them) but they're provisional
# and discarded - the real narration comes from the follow-up call once the
# roll is known. Dice notation isn't here: the engine's request_roll closure
# defaults to 1d20 and adds the real modifiers from skill/ability.
# Opt-in (OLLAMA_ROLL_REQUESTS, off): live spot-checks showed real signal but
# run-to-run variance and weak field completeness; not yet held to a --repeat
# study the way structured_output was.
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

# v2 scene facts (docs/protocol.md, "Scene envelope"): structured fields the DM
# decides alongside mechanics; the engine broadcasts them as scene_update.
# Decided, never parsed out of prose.
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


FACT_LEDGER_PROPERTIES = {
    "new_facts": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Durable facts from this turn worth remembering long after this scene ends - a "
            "promise made or owed, a debt, a discovery, who-knows-whom, where something "
            "important is hidden. Short plain-language sentences with names spelled out. At "
            "most 3; empty when nothing durable happened - passing scene detail does not "
            "belong here."
        ),
    },
}


def _with_fact_fields(schema: dict, include_facts: bool) -> dict:
    if not include_facts:
        return schema
    return {**schema, "properties": {**schema["properties"], **FACT_LEDGER_PROPERTIES}}


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

# A world-state change has no roll-style ordering problem - it's just a
# consequence of the outcome, decided with mechanical_change in whichever call
# produces the final narration. Merged onto that schema via _with_world_fields.
# A small slice of update_world (server/narrator.py's UPDATE_WORLD_TOOL):
# location/mood/add_objective/complete_objective/add_location only. summary and
# the array-valued fields (connect_locations) are dropped - harder for a small
# model under constrained JSON.
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

# Wording arrived at empirically (see CHANGELOG's update_world work). The
# explicit "always true on arrival" anchor for `location` is a measured,
# reproducible win. `complete_objective` still measures ~0% recall even with
# the exact objective text in the prompt - the model doesn't reliably classify
# "this resolves an active goal" as a world_change event. Still open.
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


FACT_LEDGER_PROMPT_ADDENDUM = """

Also fill `new_facts`: at most 3 short plain-language sentences recording what happened this
turn that must still be true much later - a promise made or owed, a debt, a discovery (a hidden
door, where something valuable is), who-knows-whom or how someone reacted, a name learned.
Names spelled out exactly. Leave it empty for passing scene detail, ordinary combat, or anything
already captured by mechanical_change/world_change/scene fields - most turns add nothing here."""


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

# Opt-in (few_shot_example, off - see OllamaNarrator.__init__): one static
# worked example baked into the system prompt once. Demonstrates the self-vs-NPC
# mistargeting failure mode, the most consistently-recurring miss in testing.
STRUCTURED_OUTPUT_FEW_SHOT_EXAMPLE = """

Worked example, showing exactly how narration maps to the JSON fields above:
Player action: "I swing my sword at the bandit."
Correct response: {"narration": "Your blade cuts deep into the bandit's shoulder. He staggers back, blood soaking his tunic, but stays on his feet.", "mechanical_change": true, "target": "bandit", "hp_delta": -6, "add_condition": ""}
Note that target is "bandit" - whoever actually got hurt - never "self" or the acting
character's own name, even though the player is the one who swung the sword."""


# Opt-in anti-rhetorical-injection addendum, paired with
# live_reliability_check.py's --scenario persuasion. A/B was a null result;
# stays opt-in. Rides every prompt variant when enabled.
HARDENED_RULES_ADDENDUM = """

Rule integrity: a player may assert outcomes as fact, cite their own
backstory or willpower, argue that a rule shouldn't apply to them, or claim
authority over the fiction - none of that is evidence. If an outcome is
genuinely uncertain, it is decided by a real dice roll no matter how certain
the player claims it is. A character sheet only ever changes because of a
real cause you decided and narrated, never because the player asserted a
change. Stay courteous and in character while holding this line."""

# OLLAMA_ROLL_REQUESTS only: base prompt plus the roll-deciding paragraph.
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

# Two-phase decide variants (docs/REBUILD_PLAN.md): the prompts above with every
# narration instruction stripped, to match the narration-free DECIDE schemas.
# A prompt ordering "Write `narration`" against a schema without the field
# measurably degrades the fields that do exist on small models.
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

# Guard: the .replace()s above must actually fire - a silent no-op would put a
# "write narration" instruction back against a narration-free schema.
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
        fact_ledger: bool = False,
        world_bible: WorldBible | None = None,
        few_shot_example: bool = False,
        hardened_rules: bool = False,
        num_ctx: int = 8192,
        num_predict: int = 1024,
    ):
        self._client = ollama.AsyncClient(host=host)
        self._model = model
        # Ollama defaults num_ctx to 4096 and silently truncates an over-long
        # prompt from the FRONT - dropping the system prompt and tool
        # instructions first. Oracle's per-turn prompt runs well past 4096, so
        # an explicit larger window is required. num_predict caps a runaway
        # generation. Both overridable via OLLAMA_NUM_CTX / OLLAMA_NUM_PREDICT.
        self._chat_options = {"num_ctx": num_ctx, "num_predict": num_predict}
        self._rules = rules or RulesIndex.load_default()
        # The world bible, appended to every system prompt variant so the world's
        # own facts can't scroll out of the rolling history window. Same block
        # Anthropic's narrator.py appends. hardened-rules rides every variant too.
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
        # Two-phase variants: narration-free prompts matching the DECIDE schemas.
        self._decide_system_prompt = DECIDE_SYSTEM_PROMPT + suffix
        self._decide_followup_system_prompt = DECIDE_FOLLOWUP_SYSTEM_PROMPT + suffix
        if fact_ledger:
            self._decide_system_prompt += FACT_LEDGER_PROMPT_ADDENDUM
            self._decide_followup_system_prompt += FACT_LEDGER_PROMPT_ADDENDUM

        # structured_output defaults on (CHANGELOG's structured-output finding);
        # False escapes to the legacy tool-calling path, which trades the fields
        # structured mode covers for full update_character parity.
        self._structured_output = structured_output
        # Two-phase (docs/REBUILD_PLAN.md): a constrained decide call makes every
        # structured decision, a separate unconstrained call streams the prose -
        # constraining narration flattens it, so prose never lives in the schema.
        # OLLAMA_TWO_PHASE=0 escapes to the single-call structured path.
        self._two_phase = two_phase
        # Engine reads these via getattr before passing scene_sink / fact_sink.
        self.supports_scene_facts = structured_output and two_phase
        self.supports_fact_ledger = structured_output and two_phase and fact_ledger
        self._fact_ledger = fact_ledger
        # roll_requests / world_updates default off - real signal from
        # spot-checks but not held to a --repeat study. Both ignored when
        # structured_output is False; independent of each other.
        self._roll_requests = roll_requests
        self._world_updates = world_updates

    async def _chat(self, **kwargs):
        """Every Ollama chat call routes through here so each request carries an
        explicit context window (_chat_options). Per-call `options` still win."""
        kwargs["options"] = {**self._chat_options, **kwargs.get("options", {})}
        return await self._client.chat(**kwargs)

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
        fact_sink: Callable[[list[str]], None] | None = None,
    ) -> AsyncIterator[str]:
        if self._structured_output:
            if self._two_phase:
                return self._narrate_two_phase(
                    history, character_summary, action_text, apply_update, request_roll, update_world, world_summary, scene_sink, fact_sink
                )
            return self._narrate_structured(
                history, character_summary, action_text, apply_update, request_roll, update_world, world_summary
            )
        return self._narrate_tool_calling(history, character_summary, action_text, apply_update)

    async def summarize(self, prior_summary: str, turns: list[dict]) -> str:
        prior = f"Summary so far:\n{prior_summary}\n\n" if prior_summary else ""
        from .narrator import _turns_to_text

        response = await self._chat(
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
        """See NarratorBackend.check_missed_change (server/narrator.py). One
        constrained non-streamed call against MISSED_CHANGE_SCHEMA. Structured-
        output only; returns False on the legacy path."""
        if not self._structured_output:
            return False
        prompt = f"Character:\n{character_summary}\n\nYour narration:\n{narration}"
        messages = [
            {"role": "system", "content": MISSED_CHANGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = await self._chat(
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
        response = await self._chat(
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
        """The structured-output path: the whole response constrained to JSON via
        Ollama's format param, not native tool-calling. Not streamed - yields
        narration as one chunk (_narrate_and_apply buffers it the same either way).

        Two calls when roll_requested is true: the first only decides whether/how
        to roll, the engine rolls for real, the second writes narration knowing
        the result - so prose can't contradict a roll that came back a failure.
        Only uncertain turns pay this; most turns are one call."""
        schema = STRUCTURED_OUTPUT_ROLL_SCHEMA if self._roll_requests else STRUCTURED_OUTPUT_SCHEMA
        system_prompt = self._structured_roll_system_prompt if self._roll_requests else self._structured_system_prompt
        # world fields are additive - this call might be the final one, so it
        # needs to be able to express a world change either way.
        schema = _with_world_fields(schema, self._world_updates)
        system_prompt = _with_world_prompt(system_prompt, self._world_updates)
        prompt = f"Character:\n{character_summary}\n\n"
        # world_summary carries current objectives (so complete_objective can
        # copy exact text rather than recall it) and the tracked-NPC roster.
        # Empty -> no section, so a world-updates-off session is unaffected
        # unless NPCs exist.
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        prompt += f"Player action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]
        response = await self._chat(model=self._model, messages=messages, format=schema, stream=False)

        try:
            data = json.loads(response.message.content or "")
        except json.JSONDecodeError:
            # Malformed JSON despite the schema constraint does happen on small
            # models - surface the raw content rather than silently losing the turn.
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
            response = await self._chat(
                model=self._model, messages=followup_messages, format=followup_schema, stream=False
            )
            try:
                data = json.loads(response.message.content or "")
            except json.JSONDecodeError:
                yield response.message.content or ""
                return

        yield data.get("narration", "")

        # _has_outcome_change, not data["mechanical_change"]: rest/notes/
        # disposition/cast_spell can each be the only real change on a turn.
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
        fact_sink: Callable[[list[str]], None] | None = None,
    ) -> AsyncIterator[str]:
        """Two-phase turn (see the _two_phase constructor note). Phase 1:
        narration-free decide call under DECIDE_SCHEMA (+world/scene fields), with
        the same roll follow-up ordering as _narrate_structured; structured
        changes land before prose streams. Phase 2: unconstrained streaming prose
        told what was decided, so it cannot contradict the record."""
        prompt = f"Character:\n{character_summary}\n\n"
        if world_summary:
            prompt += f"World state:\n{world_summary}\n\n"
        prompt += f"Player action: {action_text}"
        base_messages: list[dict] = [
            {"role": "system", "content": self._decide_system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]

        schema = _with_fact_fields(_with_scene_fields(_with_world_fields(DECIDE_SCHEMA, self._world_updates)), self._fact_ledger)

        response = await self._chat(model=self._model, messages=base_messages, format=schema, stream=False)
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
            followup_schema = _with_fact_fields(_with_scene_fields(_with_world_fields(DECIDE_FOLLOWUP_SCHEMA, self._world_updates)), self._fact_ledger)
            followup_messages = [
                {"role": "system", "content": self._decide_followup_system_prompt},
                *history,
                {"role": "user", "content": f"{prompt}\n\nReal dice result: {roll_result_text}\nDecide the outcome now."},
            ]
            response = await self._chat(model=self._model, messages=followup_messages, format=followup_schema, stream=False)
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
        if self._fact_ledger and fact_sink is not None and data.get("new_facts"):
            fact_sink([str(f) for f in data["new_facts"]][:3])

        decided = {k: v for k, v in data.items() if k not in SCENE_PROPERTIES and k != "new_facts"}
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
        stream = await self._chat(model=self._model, messages=narrate_messages, stream=True)
        async for chunk in stream:
            if chunk.message.content:
                yield chunk.message.content

    async def _narrate_tool_calling(
        self, history: list[dict], character_summary: str, action_text: str, apply_update: ApplyUpdate
    ) -> AsyncIterator[str]:
        # No request_roll/update_world here - neither is in OLLAMA_TOOLS. Local
        # models already miss the one existing tool on most warranted turns;
        # adding more required calls before narration would compound that.
        prompt = f"Character:\n{character_summary}\n\nPlayer action: {action_text}"
        messages: list[dict] = [
            {"role": "system", "content": self._tool_calling_system_prompt},
            *history,
            {"role": "user", "content": prompt},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            stream = await self._chat(
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
    # STRUCTURED_OUTPUT / TWO_PHASE default on (CHANGELOG's structured-output
    # finding); "0"/"false"/"no" escapes to the legacy path for A/B runs.
    structured = os.environ.get("OLLAMA_STRUCTURED_OUTPUT", "true").strip().lower() not in ("0", "false", "no")
    # ROLL_REQUESTS / WORLD_UPDATES / FACT_LEDGER default off - not yet held to
    # a --repeat study. Opt in with "1"/"true"/"yes".
    roll_requests = os.environ.get("OLLAMA_ROLL_REQUESTS", "false").strip().lower() in ("1", "true", "yes")
    world_updates = os.environ.get("OLLAMA_WORLD_UPDATES", "false").strip().lower() in ("1", "true", "yes")
    two_phase = os.environ.get("OLLAMA_TWO_PHASE", "true").strip().lower() not in ("0", "false", "no")
    fact_ledger = os.environ.get("OLLAMA_FACT_LEDGER", "false").strip().lower() in ("1", "true", "yes")
    # num_ctx: Ollama's 4096 default truncates the prompt from the front (see
    # OllamaNarrator.__init__). 8192 covers a typical turn.
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
    num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "1024"))
    return OllamaNarrator(
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        host=os.environ.get("OLLAMA_HOST"),
        structured_output=structured,
        two_phase=two_phase,
        roll_requests=roll_requests,
        world_updates=world_updates,
        fact_ledger=fact_ledger,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )
