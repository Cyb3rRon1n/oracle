from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from shared.protocol import Envelope

from .transport import ClientTransport

# Mirrors server/engine.py's CLASS_STARTING_EQUIPMENT keys - the SRD dataset
# (server/rules/srd.json) is the real source of truth for what a class
# grants; this is just the same short list for the prompt. An unrecognized
# or blank entry falls back gracefully server-side, so this isn't strictly
# validated here.
CHARACTER_CLASSES = ["fighter", "wizard", "rogue", "cleric"]

# A short, optional class-recommendation quiz on the welcome screen (a
# direct owner ask - not asked of everyone, just offered as a "not sure?"
# path). Purely client-side and purely a suggestion: WelcomeScreen._join
# still sends whatever #class-input actually contains when Join is
# pressed, exactly like a manually-typed class - the survey only ever
# pre-fills that same field, never bypasses it. Each option maps 1:1 to
# one of CHARACTER_CLASSES above; the class with the most picks across
# all three questions wins, ties broken by CHARACTER_CLASSES' own order
# (deterministic, not dict-iteration-order luck).
CLASS_SURVEY_QUESTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "When trouble finds you, you're most likely to...",
        [
            ("Meet it head-on", "fighter"),
            ("Talk or sneak your way around it", "rogue"),
            ("Already have a plan worked out", "wizard"),
            ("Trust something bigger than yourself to see you through", "cleric"),
        ],
    ),
    (
        "Your idea of a good day is...",
        [
            ("Testing your strength against something worthy", "fighter"),
            ("Finding an angle nobody else saw", "rogue"),
            ("Learning something new and dangerous", "wizard"),
            ("Helping someone who needed it", "cleric"),
        ],
    ),
    (
        "If a friend was in real danger, you'd...",
        [
            ("Put yourself between them and the danger", "fighter"),
            ("Get them out before anyone noticed", "rogue"),
            ("Already be working the problem", "wizard"),
            ("Pray it's not too late, and act anyway", "cleric"),
        ],
    ),
]

# A direct owner ask: an "outcome" log_entry's mechanical category
# (server/engine.py's _outcome_category) should read differently at a
# glance - red for damage, green for healing, etc. - rather than every
# combat/item/spell effect blending into the same plain log text. Kept as
# a flat lookup, not styling logic scattered across call sites, so the
# palette is defined exactly once.
_OUTCOME_COLORS = {
    "damage": "red",
    "heal": "green",
    "spell": "cyan",
    "condition": "magenta",
    "item": "yellow",
}


def _recommend_class(tally: dict[str, int]) -> str | None:
    """Picks the class-survey's winner from a {class_name: pick_count}
    tally - a plain function, not a WelcomeScreen method, so it's testable
    without a real Textual pilot. None if every question was left
    unanswered (an empty or all-zero tally), matching "an unstarted quiz
    recommends nothing" rather than an arbitrary default. Ties broken by
    CHARACTER_CLASSES' own fixed order (max() takes the first max it
    sees), not dict-iteration-order luck."""
    if not any(tally.values()):
        return None
    return max(CHARACTER_CLASSES, key=lambda c: tally.get(c, 0))


def _load_character_file(path: str) -> tuple[dict | None, str | None]:
    """Reads and parses a character export file for WelcomeScreen's optional
    import field - local file I/O, so this happens client-side before
    join_session is even sent. The server still independently validates the
    shape (server/engine.py's _character_from_import) since a client is
    never a trusted boundary for another connection's data - this is just
    the "can we even read and parse it" check, so a bad path fails fast
    with a clear message instead of silently sending garbage to the server.
    Returns (data, None) on success or (None, error_message) on any
    failure - a missing file, invalid JSON, or valid JSON that isn't even
    an object all become one user-facing message rather than a raw
    traceback, the same graceful-fallback spirit build_starting_character's
    blank/unrecognized-class handling already established server-side."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError:
        return None, f"Couldn't read '{path}' - check the path and try again."
    except json.JSONDecodeError:
        return None, f"'{path}' isn't valid JSON."
    if not isinstance(data, dict):
        return None, f"'{path}' doesn't look like a character file."
    return data, None


def _transcript_text(rich_log: RichLog) -> str:
    """Plain text of everything currently in a RichLog, one line per Strip.
    Strip.text already discards Rich markup/styling, leaving just the
    characters a player actually reads on screen - the same technique the
    test suite's own _log_text helper already relies on. RichLog's own
    max_lines defaults to unbounded (none of this client's RichLog
    instances set it), so .lines genuinely holds the whole session's
    accumulated log for the lifetime of the widget, not just what's
    currently scrolled into view."""
    return "\n".join(strip.text for strip in rich_log.lines)


class CharacterSheetPanel(Vertical):
    """A tabbed character sheet (ROADMAP.md item 7) - Overview/Abilities/
    Inventory/Spells/Features & Notes, replacing the original single
    scrolling Static blob so there's real room for detail (class features,
    skill proficiencies) that had nowhere to live before. Each tab is its
    own Static, rebuilt in full on every update (not lazily on tab switch)
    so a tab you're not currently looking at is never stale by the time
    you do switch to it."""

    # pageup/pagedown, not ctrl+left/right as ROADMAP.md's own design pass
    # first suggested - Input (the action/chat box, where focus normally
    # sits) already binds ctrl+left/ctrl+right for word-cursor movement,
    # so that choice would have silently stolen a real editing shortcut
    # from the widget doing the actual work. Checked against Textual's own
    # Input BINDINGS before picking these instead of assuming.
    BINDINGS = [
        ("pageup", "previous_tab", "Prev tab"),
        ("pagedown", "next_tab", "Next tab"),
    ]

    _TAB_IDS = ("tab-overview", "tab-map", "tab-abilities", "tab-inventory", "tab-spells", "tab-features")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._character: dict = {}
        self._world: dict = {}
        self._others: dict = {}
        self._npcs: dict = {}

    def compose(self) -> ComposeResult:
        # Each pane's Static sits inside its own VerticalScroll - the panel
        # itself now lives in a fixed-height horizontal band (LobbyScreen/
        # SessionScreen CSS, below the now-full-width log) rather than a
        # tall side column, so a longer sheet (many known spells, a big
        # inventory) needs to scroll internally instead of just being fully
        # visible at a glance the way the old tall column allowed.
        with TabbedContent(id="sheet-tabs", initial="tab-overview"):
            with TabPane("Overview", id="tab-overview"), VerticalScroll():
                yield Static(id="tab-overview-content")
            with TabPane("Map", id="tab-map"), VerticalScroll():
                yield Static(id="tab-map-content")
            with TabPane("Abilities", id="tab-abilities"), VerticalScroll():
                yield Static(id="tab-abilities-content")
            with TabPane("Inventory", id="tab-inventory"), VerticalScroll():
                yield Static(id="tab-inventory-content")
            with TabPane("Spells", id="tab-spells"), VerticalScroll():
                yield Static(id="tab-spells-content")
            with TabPane("Features & Notes", id="tab-features"), VerticalScroll():
                yield Static(id="tab-features-content")

    def action_previous_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def _is_tab_visible(self, tab_id: str) -> bool:
        if tab_id == "tab-spells":
            return bool(self._character.get("known_spells"))
        if tab_id == "tab-map":
            # Hidden until the DM has registered at least one location -
            # an empty map tab would just be dead space on every session
            # until the DM ever calls add_location/connect_locations.
            return bool(self._world.get("location_map"))
        return True

    def _cycle_tab(self, direction: int) -> None:
        visible = [tab_id for tab_id in self._TAB_IDS if self._is_tab_visible(tab_id)]
        tabs = self.query_one("#sheet-tabs", TabbedContent)
        if tabs.active not in visible:
            return
        index = visible.index(tabs.active)
        tabs.active = visible[(index + direction) % len(visible)]

    def render_sheet(self, character: dict) -> None:
        self._character = character
        self._refresh_display()

    def render_world(self, world: dict) -> None:
        self._world = world
        self._refresh_display()

    def render_npcs(self, npcs: dict) -> None:
        # npcs is keyed by name -> its tracked sheet (server/state.py's
        # CharacterSheet, same shape NPCs always used - no public/private
        # split needed here, NPCs have no private owner at all, see
        # docs/protocol.md's "Private vs. shared state"). Real, persistent
        # NPC status here - replacing the "dim log line" first-pass UI
        # footprint (still written too, see _npc_status_line below, as a
        # narrative beat) with a view that stays current instead of
        # requiring a scroll back through the log to find the latest state.
        self._npcs = npcs
        self._refresh_display()

    def render_others(self, others: dict) -> None:
        # others is keyed by player_id -> that player's public view
        # (name/character_class/hp/max_hp/conditions - never inventory,
        # same boundary the server's own public/private split enforces).
        self._others = others
        self._refresh_display()

    def _overview_text(self) -> str:
        character = self._character
        name_line = f"[b]{character.get('name', '?')}[/b]"
        character_class = character.get("character_class")
        if character_class:
            name_line += f" ({character_class})"
        # "Lv N" alongside class - the same header real sheets put level in
        # (D&D Beyond shows it right next to class on the summary panel).
        # level defaults to 1 rather than being omitted when absent, since
        # every real character sheet payload carries it (server/state.py's
        # CharacterSheet.level default) - only a genuinely bare/legacy dict
        # (e.g. an NPC view, which has no level at all) would fall through
        # to the default here.
        name_line += f"  Lv {character.get('level', 1)}"
        lines = [
            name_line,
            self._hp_line(
                character.get("hp"), character.get("max_hp"), character.get("ac"),
                dying=character.get("dying", False), dead=character.get("dead", False),
            ),
        ]
        # xp is only ever present on the owner's own full sheet (never on
        # an "others"/party entry - server/engine.py's _public_character_view
        # deliberately keeps it private, same boundary inventory/stats/notes
        # already have), so this line only shows up on your own sheet.
        if "xp" in character:
            lines.append(f"[dim]XP: {character.get('xp', 0)}[/dim]")

        active_objectives = [o for o in (self._world.get("objectives") or []) if o.get("status") == "active"]
        if active_objectives:
            lines.append("")
            lines.append("[b]Objectives[/b]")
            lines.extend(f"- {o['text']}" for o in active_objectives)

        if self._npcs:
            lines.append("")
            lines.append(self._DIVIDER)
            lines.append("[b]NPCs[/b]")
            lines.extend(self._npc_line(name, npc) for name, npc in self._npcs.items())

        if self._others:
            # "Party", not "Other Players" - the genre-standard term both
            # D&D Beyond's Campaign dashboard and Roll20's turn-order
            # tracker use for this exact "everyone else, at a glance" view,
            # as opposed to a single character's own full sheet.
            lines.append("")
            lines.append(self._DIVIDER)
            lines.append("[b]Party[/b]")
            lines.extend(self._other_player_line(other) for other in self._others.values())

        return "\n".join(lines).rstrip()

    def _abilities_text(self) -> str:
        character = self._character
        stats = character.get("stats") or {}
        lines: list[str] = []
        if stats:
            # stat_modifiers is a server-side @computed_field (real
            # precomputed modifiers, server/state.py) - present whenever
            # stats is, so this never recomputes floor((score-10)/2)
            # client-side. A fixed str/dex/con/int/wis/cha order, not dict
            # iteration order - stats is keyed the same way SRD monster
            # blocks already are, but a client shouldn't assume any
            # particular dict insertion order survived JSON round-tripping.
            modifiers = character.get("stat_modifiers") or {}
            lines.append("[b]Ability Scores[/b]")
            lines.extend(
                f"{key.upper()} {stats[key]} ({'+' if modifiers.get(key, 0) >= 0 else ''}{modifiers.get(key, 0)})"
                for key in ("str", "dex", "con", "int", "wis", "cha")
                if key in stats
            )
        # skill_proficiencies is owner-only, added by server/engine.py's
        # _owner_character_view (ROADMAP.md item 7) - previously this was
        # only ever visible transiently in a roll's own label text after
        # the fact, never as something a player could just look at.
        skill_proficiencies = character.get("skill_proficiencies") or []
        if skill_proficiencies:
            if lines:
                lines.append("")
            lines.append("[b]Skill Proficiencies[/b]")
            lines.extend(f"- {skill.replace('_', ' ').title()}" for skill in skill_proficiencies)
        return "\n".join(lines).rstrip()

    def _inventory_text(self) -> str:
        # Equipped (weapon/armor - real slots, server/state.py's
        # equipped_weapon/equipped_armor) separate from Carried (everything
        # else you own), not one flat list - a direct owner ask ("not just
        # a general listing of items"), and the same "closely resemble a
        # real tabletop sheet" reasoning driving this tab split in the
        # first place. equipped_weapon/armor are still just names in
        # inventory, not their own separate store - carried is inventory
        # minus whichever of those two are currently set.
        character = self._character
        inventory = character.get("inventory") or []
        if not inventory:
            return ""
        equipped_weapon = character.get("equipped_weapon")
        equipped_armor = character.get("equipped_armor")
        lines = ["[b]Equipped[/b]"]
        lines.append(f"Weapon: {equipped_weapon}" if equipped_weapon else "Weapon: [dim](none)[/dim]")
        lines.append(f"Armor: {equipped_armor}" if equipped_armor else "Armor: [dim](none)[/dim]")
        carried = [item for item in inventory if item not in (equipped_weapon, equipped_armor)]
        if carried:
            lines.append("")
            lines.append("[b]Carried[/b]")
            lines.extend(f"- {item}" for item in carried)
        return "\n".join(lines).rstrip()

    def _spells_text(self) -> str:
        # known_spells/spell_slots/spell_save_dc are only ever present on
        # the owner's own full sheet (server/engine.py's
        # _public_character_view keeps them private, same boundary
        # inventory/stats already have) - the client has no SRD spell data
        # of its own (that's server-only), so this can't distinguish a
        # cantrip from a leveled spell in the list below - just the flat
        # known list plus the real slot counts already sent.
        character = self._character
        known_spells = character.get("known_spells") or []
        if not known_spells:
            return ""
        dc = character.get("spell_save_dc")
        lines = ["[b]Spells[/b]" + (f" (DC {dc})" if dc is not None else "")]
        slots = character.get("spell_slots") or {}
        max_slots = character.get("max_spell_slots") or {}
        if max_slots:
            slot_text = ", ".join(
                f"{level} {slots.get(level, 0)}/{count}"
                for level, count in sorted(max_slots.items(), key=lambda kv: int(kv[0]))
            )
            lines.append(f"[dim]Slots: {slot_text}[/dim]")
        lines.extend(f"- {name.replace('_', ' ').title()}" for name in known_spells)
        return "\n".join(lines).rstrip()

    def _features_text(self) -> str:
        character = self._character
        lines: list[str] = []
        # background is owner-only (server/state.py's CharacterSheet,
        # generated once at creation by server/engine.py's
        # build_starting_character via server/lore's random_origin) - who
        # this character was before Aetherfall. Blank for a sheet that
        # predates this field (an old sessions/*.json) or an imported
        # character (_character_from_import doesn't set it) - same
        # "don't render the absent default" rule every other optional
        # section here already follows.
        background = character.get("background")
        if background:
            lines.append("[b]Background[/b]")
            lines.append(background)
        # class_features is owner-only, added by server/engine.py's
        # _owner_character_view (ROADMAP.md item 7) - real SRD data
        # (server/rules/srd.json's level_1_features) that existed all
        # along but was never actually sent to the client before this.
        class_features = character.get("class_features") or []
        if class_features:
            lines.append("[b]Class Features[/b]")
            lines.extend(f"- {feature}" for feature in class_features)
        conditions = character.get("conditions") or []
        if conditions:
            if lines:
                lines.append("")
            lines.append("[b]Conditions[/b]")
            lines.extend(f"- {c}" for c in conditions)
        # notes is set via /note (character_edit) but was never actually
        # rendered anywhere on the sheet before this tab existed - a real
        # pre-existing gap this closes, not new functionality.
        notes = character.get("notes")
        if notes:
            if lines:
                lines.append("")
            lines.append("[b]Notes[/b]")
            lines.append(notes)
        return "\n".join(lines).rstrip()

    def _map_text(self) -> str:
        # location_map is a plain adjacency list (ROADMAP.md item 8) - no
        # coordinates, so this renders each known location and its real
        # exits rather than attempting a 2D layout, which would need the
        # DM to supply consistent x/y positions this project doesn't ask
        # for. Shared world state, not owner-only (unlike stats/inventory) -
        # every player sees the same map, matching world_state's existing
        # broadcast-to-everyone treatment.
        location_map = self._world.get("location_map") or {}
        if not location_map:
            return ""
        current = self._world.get("location")
        lines = ["[b]Map[/b]"]
        for name in sorted(location_map):
            marker = " [dim](here)[/dim]" if name == current else ""
            lines.append(f"{name}{marker}")
            lines.extend(f"  -> {exit_name}" for exit_name in location_map.get(name) or [])
        return "\n".join(lines).rstrip()

    def all_text(self) -> str:
        """Every tab's content concatenated - for tests/tooling that just
        need to check whether some text appears anywhere on the sheet,
        regardless of which tab it lives on."""
        return "\n".join(
            [
                self._overview_text(),
                self._map_text(),
                self._abilities_text(),
                self._inventory_text(),
                self._spells_text(),
                self._features_text(),
            ]
        )

    def _refresh_display(self) -> None:
        # Deliberately not named _render - Textual's own Widget._render() is
        # a real internal method (returns a Visual for the content-height
        # pipeline), and a same-named subclass method silently shadows it
        # instead of erroring at definition time. That collision is exactly
        # what crashed this app on startup against Textual 8.2.8's newer
        # internals (pyproject.toml only pins textual>=0.58, no upper bound)
        # - self._render() returning None broke get_content_height()'s
        # visual.get_height() call.
        try:
            self.query_one("#tab-overview-content", Static).update(self._overview_text())
            self.query_one("#tab-map-content", Static).update(self._map_text())
            self.query_one("#tab-abilities-content", Static).update(self._abilities_text())
            self.query_one("#tab-inventory-content", Static).update(self._inventory_text())
            self.query_one("#tab-spells-content", Static).update(self._spells_text())
            self.query_one("#tab-features-content", Static).update(self._features_text())
        except NoMatches:
            # A render call can land before compose()'s children are
            # mounted (e.g. a very first state_sync racing on_mount) - a
            # no-op here is fine, on_mount's own refresh_sheet_widgets call
            # covers the real first paint once mounting completes.
            return

        tabs = self.query_one("#sheet-tabs", TabbedContent)
        for tab_id in ("tab-spells", "tab-map"):
            if self._is_tab_visible(tab_id):
                tabs.show_tab(tab_id)
            else:
                tabs.hide_tab(tab_id)

    _DIVIDER = "[dim]" + "─" * 24 + "[/dim]"

    @staticmethod
    def _hp_bar(hp: int | None, max_hp: int | None, width: int = 10) -> str:
        # The green/yellow/red-by-fraction health bar almost every VTT uses
        # for token HP at a glance (Roll20's token bars chief among them) -
        # adapted here to plain block characters since this is a terminal
        # UI with no bar widgets/images to draw on.
        hp = hp or 0
        max_hp = max_hp or 0
        fraction = (hp / max_hp) if max_hp else 0
        filled = max(0, min(width, round(fraction * width)))
        bar = "█" * filled + "░" * (width - filled)
        color = "green" if fraction > 0.5 else "yellow" if fraction > 0.25 else "red"
        return f"[{color}]{bar}[/{color}]"

    @staticmethod
    def _death_status_label(hp: int | None, dying: bool = False, dead: bool = False) -> str:
        # hp == 0 without dying/dead means stabilized (3 death-save
        # successes reached, or the same-turn edge case in
        # CharacterSheet.apply_update where a heal and a drop-to-0 land in
        # the same update) - still unconscious, just no longer at risk,
        # a real third state distinct from both DYING and a normal 0 read
        # as "about to die".
        if dead:
            return "  [b red]DEAD[/b red]"
        if dying:
            return "  [b red]DYING[/b red]"
        if (hp or 0) == 0:
            return "  [yellow]STABLE[/yellow]"
        return ""

    @classmethod
    def _hp_line(
        cls, hp: int | None, max_hp: int | None, ac: int | None = None,
        dying: bool = False, dead: bool = False,
    ) -> str:
        # ac is optional (an NPC status line has no ac at all pre-this-
        # feature callers, and a genuinely bare/legacy dict might not
        # carry one either) - omitted rather than shown as "AC 0", a real
        # value some future all-armor-stripped character could otherwise
        # be confused with.
        ac_label = f"  AC {ac}" if ac is not None else ""
        status_label = cls._death_status_label(hp, dying, dead)
        return f"HP {hp or 0}/{max_hp or 0}  {cls._hp_bar(hp, max_hp)}{ac_label}{status_label}"

    @classmethod
    def _other_player_line(cls, other: dict) -> str:
        line = f"- {other.get('name', '?')}"
        character_class = other.get("character_class")
        if character_class:
            line += f" ({character_class})"
        line += f" Lv{other.get('level', 1)}"
        hp, max_hp = other.get("hp"), other.get("max_hp")
        # A shorter bar than the main sheet's own - a party glance is meant
        # to be compact, the same "your own sheet gets full detail, the
        # party list gets a quick read" split Roll20's turn-order tracker
        # (small per-token bars) makes relative to your own character sheet.
        line += f": HP {hp}/{max_hp} {cls._hp_bar(hp, max_hp, width=6)}"
        ac = other.get("ac")
        if ac is not None:
            line += f" AC {ac}"
        line += cls._death_status_label(hp, other.get("dying", False), other.get("dead", False))
        conditions = other.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line

    @classmethod
    def _npc_line(cls, name: str, npc: dict) -> str:
        hp, max_hp = npc.get("hp"), npc.get("max_hp")
        line = f"- {name}: HP {hp}/{max_hp} {cls._hp_bar(hp, max_hp, width=6)}"
        ac = npc.get("ac")
        if ac is not None:
            line += f" AC {ac}"
        # Never STABLE/DYING/DEAD - _death_status_label's trio is player-
        # only (real 5e death saves, see "Death saves" in docs/protocol.md).
        # An NPC dies outright at 0 HP with no stabilize concept at all
        # (server/engine.py's own XP-on-defeat trigger), so a defeated
        # monster gets its own, simpler label instead of reusing a label
        # set that would misleadingly imply it could still come back.
        if (hp or 0) == 0:
            line += "  [dim](defeated)[/dim]"
        disposition = npc.get("disposition")
        if disposition and disposition != "neutral":
            line += f" {disposition}"
        conditions = npc.get("conditions") or []
        if conditions:
            line += f" ({', '.join(conditions)})"
        return line


def _npc_status_line(name: str, npc: dict) -> str:
    line = f"[dim]{name}: HP {npc.get('hp')}/{npc.get('max_hp')}"
    ac = npc.get("ac")
    if ac is not None:
        line += f" AC {ac}"
    # "neutral" is the field's own default and the common, uninteresting
    # case (most NPCs are never given a disposition at all) - only show it
    # once the DM has actually set something worth knowing, the same
    # "don't render the boring default" rule conditions/inventory already
    # follow by only appearing when non-empty. Plain text, not bracketed
    # (e.g. "[hostile]") - RichLog is markup=True, and Rich's own markup
    # parser treats square brackets as a style tag, silently swallowing an
    # unrecognized one like "[hostile]" from the rendered output entirely -
    # a real bug caught by running this, not assumed safe.
    disposition = npc.get("disposition")
    if disposition and disposition != "neutral":
        line += f" {disposition}"
    conditions = npc.get("conditions") or []
    if conditions:
        line += f" ({', '.join(conditions)})"
    return line + "[/dim]"


class WelcomeScreen(Screen):
    """The client's very first screen - collects identity (name, session
    ID, and, for a genuinely new local player, class) and joins. Replaces
    client/main.py's old blocking input() prompts, which can't run inside
    a live Textual event loop anyway - same information, gathered as real
    widgets instead of stdin lines."""

    CSS = """
    WelcomeScreen { align: center middle; }
    #welcome-box { width: 50; height: auto; max-height: 100%; border: solid $accent; padding: 0 2; }
    #welcome-box Input { margin-bottom: 1; }
    #welcome-box #import-input { margin-bottom: 0; }
    #welcome-box RadioSet { margin-bottom: 1; border: none; padding: 0; }
    #welcome-error { color: $error; height: 1; }
    /* Compact, borderless - a full bordered Button's/RadioSet's default
    height (both border: tall by default, +2 rows each) would push #join
    past this screen's own scrollable viewport at a standard 80x24
    terminal (a real thing found by running this, not assumed - see the
    many pilot.click("#join") tests this would otherwise send out of the
    visible region entirely). */
    #survey-toggle { border: none; height: 1; min-height: 1; padding: 0; margin-bottom: 0; }
    #class-survey RadioSet { margin-bottom: 0; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        # VerticalScroll, not Vertical - the two new import-related widgets
        # (added alongside the existing class prompt) can push a new-
        # character join past a standard 80x24 viewport's visible height;
        # max-height: 100% above caps the box so it scrolls internally
        # instead of pushing #join off-screen, the same real fix this
        # workspace's other Textual projects have needed for the identical
        # "adding fields to an already-tall screen" class of bug.
        with VerticalScroll(id="welcome-box"):
            yield Static("[b]Oracle[/b] - an AI Dungeon Master")
            yield Input(placeholder="Character name", id="name-input")
            # Solo (default) mints a guaranteed-fresh session on Join (see
            # _join() below) - a real, repeatedly-hit point of confusion
            # this closes: a blank/reused Session ID could silently land a
            # solo player behind other characters already in an existing
            # session's turn order, submitting an action into "It's not
            # your turn" with no visible explanation why, particularly
            # against a session with an orphaned character nobody's
            # actively playing (its turn never comes back around at all).
            # Multiplayer reveals the Session ID field - same ID as
            # whoever else is joining is what actually puts you in the
            # same game together.
            with RadioSet(id="mode-select"):
                yield RadioButton("Solo game (always your own turn)", value=True, id="mode-solo")
                yield RadioButton("Multiplayer (join or host with a Session ID)", id="mode-multiplayer")
            yield Input(
                placeholder="Session ID - use the same one as whoever you're playing with",
                id="session-input",
            )
            if self.app.is_new_character:
                yield Static(f"Class ({'/'.join(CHARACTER_CLASSES)}, blank to skip)")
                yield Input(placeholder="Class", id="class-input")
                yield Button("Not sure? Get a class recommendation", id="survey-toggle")
                # Hidden until #survey-toggle is pressed - a direct owner
                # ask, but not asked of everyone by default, matching the
                # existing "recommend, don't require" tone every other
                # engine-suggested-but-overridable choice in this project
                # already has (structured output's own request_roll/
                # world_updates flags, the class field itself). Purely a
                # suggestion: answering never bypasses #class-input, only
                # pre-fills it, so it stays exactly as editable/clearable
                # as if it had been typed by hand.
                with Vertical(id="class-survey"):
                    for qi, (question, options) in enumerate(CLASS_SURVEY_QUESTIONS):
                        yield Static(question)
                        with RadioSet(id=f"survey-q{qi}"):
                            for label, class_name in options:
                                yield RadioButton(label, id=f"survey-q{qi}-{class_name}")
                    yield Static("", id="survey-result")
                # Import-time only, same as class - a returning character
                # already has everything an export would carry, so there's
                # nothing to import into it. A filled-in path here makes
                # name/class above irrelevant server-side (the imported
                # sheet wins - see server/engine.py's
                # _character_from_import). No separate label Static here
                # (unlike class-input above) - the placeholder alone says
                # enough, and this screen's row budget is already tight on
                # a standard 80x24 terminal (see the VerticalScroll note
                # on #welcome-box above).
                yield Input(placeholder="Import character .json (optional, overrides name/class)", id="import-input")
            yield Button("Join", id="join", variant="primary")
            yield Static("", id="welcome-error")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()
        self.query_one("#session-input", Input).display = False  # Solo is the default selection
        if self.app.is_new_character:
            self.query_one("#class-survey", Vertical).display = False

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "mode-select":
            self.query_one("#session-input", Input).display = event.pressed.id == "mode-multiplayer"
        elif event.radio_set.id and event.radio_set.id.startswith("survey-q"):
            self._update_class_recommendation()

    def _update_class_recommendation(self) -> None:
        tally: dict[str, int] = {}
        for qi in range(len(CLASS_SURVEY_QUESTIONS)):
            pressed = self.query_one(f"#survey-q{qi}", RadioSet).pressed_button
            if pressed is None or pressed.id is None:
                continue  # this question hasn't been answered yet - fine, tally what's answered so far
            class_name = pressed.id.rsplit("-", 1)[-1]
            tally[class_name] = tally.get(class_name, 0) + 1

        recommended = _recommend_class(tally)
        if recommended is None:
            return
        self.query_one("#class-input", Input).value = recommended
        self.query_one("#survey-result", Static).update(
            f"[dim]Recommended: {recommended.capitalize()} - edit the Class field above to change it.[/dim]"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "join":
            await self._join()
        elif event.button.id == "survey-toggle":
            survey = self.query_one("#class-survey", Vertical)
            survey.display = not survey.display

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._join()

    async def _join(self) -> None:
        join_button = self.query_one("#join", Button)
        if join_button.disabled:
            return  # a real Enter-then-click double-submit shouldn't send join_session twice
        join_button.disabled = True

        name = self.query_one("#name-input", Input).value.strip() or "Adventurer"
        if self.query_one("#mode-multiplayer", RadioButton).value:
            session_id = self.query_one("#session-input", Input).value.strip() or "default"
        else:
            # Derived from this client's own stable player_id (.player_id
            # on disk, see client/main.py), not a fresh random id each
            # join - a genuinely new session every time would prevent ever
            # resuming a solo game, the same continuity .player_id already
            # gives a returning character. Still guaranteed to never
            # collide with "default" or anyone else's session (player_id
            # is already a random per-client uuid), so Solo can never
            # silently land behind an orphaned character in someone else's
            # stale turn order - the actual bug this mode exists to avoid.
            session_id = f"solo-{self.app.player_id}"
        character_class = ""
        imported_character = None
        if self.app.is_new_character:
            character_class = self.query_one("#class-input", Input).value.strip()
            import_path = self.query_one("#import-input", Input).value.strip()
            if import_path:
                imported_character, error = _load_character_file(import_path)
                if error:
                    join_button.disabled = False
                    self.query_one("#welcome-error", Static).update(f"[red]{error}[/red]")
                    return

        try:
            await self.app.connect_and_join(name, session_id, character_class, imported_character)
        except OSError as exc:
            join_button.disabled = False
            self.query_one("#welcome-error", Static).update(f"[red]Couldn't connect: {exc}[/red]")


class LobbyScreen(Screen):
    """The pre-game menu: review your own character, see who else has
    joined (Party), chat before the adventure begins, and trigger the real
    start. Owner's own framing: "should there be a main menu first where
    players can join, chat, create or load their character and or review
    their character... then when start dm begins narrating the scene."""

    CSS = """
    LobbyScreen RichLog { height: 1fr; border: solid $accent; }
    LobbyScreen CharacterSheetPanel { height: 12; border: solid $accent; padding: 0 1; }
    LobbyScreen CharacterSheetPanel TabbedContent { height: 1fr; }
    #lobby-status { height: 1; padding: 0 1; }
    /* Compact, borderless - same "RadioSet's own border:tall default eats
    two rows it doesn't need" fix WelcomeScreen's #welcome-box RadioSet
    already applies. */
    #content-preference { height: 3; border: none; padding: 0; margin-bottom: 0; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        # Chat log full-width on top (majority of the screen - it's the
        # primary content, and RichLog's own wrap benefits from the extra
        # width more than the sheet's list-shaped content does), the sheet
        # (with its own tab-label row, now genuinely visible instead of
        # tucked into a side column) as a compact band underneath.
        yield RichLog(id="chat-log", wrap=True, markup=True)
        yield CharacterSheetPanel(id="sheet")
        # A real tabletop practice (agreeing on tone/intensity before play
        # begins), not previously offered at all - whoever presses Start
        # sets it for the whole session (Session.content_preference,
        # server/state.py). "Standard" is the default and needs no
        # explanation; WorldBible's own tone_guidance already covers it.
        with RadioSet(id="content-preference"):
            yield RadioButton("Lighter tone", id="pref-lighter")
            yield RadioButton("Standard tone", value=True, id="pref-standard")
            yield RadioButton("Intense tone", id="pref-intense")
        yield Static("", id="lobby-status")
        yield Button("Start Adventure", id="start", variant="success")
        yield Input(
            placeholder="Chat with the party... (/export [file] for your character, /transcript [file] for this chat)",
            id="chat-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.app.refresh_sheet_widgets()
        self.query_one("#chat-input", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.query_one("#start", Button).disabled = True
            self.query_one("#lobby-status", Static).update("[dim]Starting the adventure...[/dim]")
            pressed = self.query_one("#content-preference", RadioSet).pressed_button
            content_preference = (pressed.id or "").removeprefix("pref-") if pressed else "standard"
            await self.app.transport.send("start_session", {"content_preference": content_preference})

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/export"):
            filename = text[len("/export"):].strip() or "character"
            message = await self.app.export_character(filename)
            self.query_one("#chat-log", RichLog).write(f"[dim]{message}[/dim]")
            return

        if text.startswith("/transcript"):
            # Reads #chat-log specifically - the lobby's own separate
            # RichLog, distinct from SessionScreen's #log. A lobby-only
            # chat transcript (pre-adventure banter/character review) is a
            # genuinely different record from the in-session narration one,
            # so this deliberately isn't "the same transcript, exported
            # early" - each screen's /transcript only ever sees its own log.
            filename = text[len("/transcript"):].strip() or "lobby-chat"
            path = Path(filename)
            if path.suffix != ".txt":
                path = path.with_suffix(".txt")
            try:
                path.write_text(_transcript_text(self.query_one("#chat-log", RichLog)))
            except OSError as exc:
                self.query_one("#chat-log", RichLog).write(f"[dim]Couldn't save transcript: {exc}[/dim]")
            else:
                self.query_one("#chat-log", RichLog).write(f"[dim]Transcript saved to {path}[/dim]")
            return

        await self.app.transport.send("chat_message", {"text": text})


class SessionScreen(Screen):
    """The real in-session view - narrative log, character sheet/party,
    and the turn input. Everything client/app.py's App used to compose
    directly before the lobby existed; unchanged in content, just moved
    into its own Screen, pushed once session_started arrives."""

    CSS = """
    SessionScreen RichLog { height: 1fr; border: solid $accent; }
    SessionScreen CharacterSheetPanel { height: 12; border: solid $accent; padding: 0 1; }
    SessionScreen CharacterSheetPanel TabbedContent { height: 1fr; }
    #status { height: 1; padding: 0 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._narration_buffer = ""

    def compose(self) -> ComposeResult:
        yield Header()
        # Narration log full-width on top, sheet as a compact band below -
        # see LobbyScreen's identical layout for the reasoning.
        yield RichLog(id="log", wrap=True, markup=True)
        yield CharacterSheetPanel(id="sheet")
        yield Static("", id="status")
        yield Input(
            placeholder="What do you do? (/roll, /chat, /note, /item add|remove, /equip, /unequip, /export, /transcript [file])",
            id="input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.app.refresh_sheet_widgets()
        log = self.query_one("#log", RichLog)
        for name, npc in self.app.npcs.items():
            log.write(_npc_status_line(name, npc))
        for entry in self.app.log_tail:
            log.write(entry.get("text", ""))
        self.query_one("#input", Input).focus()

    def set_thinking(self, thinking: bool) -> None:
        self.query_one("#status", Static).update("[dim]The DM is thinking...[/dim]" if thinking else "")

    def write_log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def handle_narration_chunk(self, text: str, done: bool) -> None:
        self.set_thinking(False)  # the silent gap this fills ends the moment real text starts arriving
        if done:
            self.write_log(self._narration_buffer)
            self._narration_buffer = ""
        else:
            self._narration_buffer += text

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/roll "):
            dice, _, reason = text[len("/roll "):].strip().partition(" ")
            await self.app.transport.send("dice_roll", {"dice": dice, "reason": reason})
        elif text.startswith("/deathsave"):
            # Empty payload - the character being rolled for is always the
            # sender's own (server/engine.py's _on_death_save), the same
            # "no payload needed, sender_id says who" shape start_session
            # already uses.
            await self.app.transport.send("death_save", {})
        elif text.startswith("/combat start"):
            # Empty payload, same shape as start_session/death_save - any
            # joined player may trigger this, not just whoever's turn it is.
            await self.app.transport.send("start_combat", {})
        elif text.startswith("/combat end"):
            await self.app.transport.send("end_combat", {})
        elif text.startswith("/chat "):
            await self.app.transport.send("chat_message", {"text": text[len("/chat "):].strip()})
        elif text.startswith("/note "):
            # notes/inventory bookkeeping, not adjudicated by the DM -
            # server/engine.py's _on_character_edit is the only handler,
            # exempt from turn order the same as /roll and /chat.
            await self.app.transport.send(
                "character_edit", {"field": "notes", "value": text[len("/note "):].strip()}
            )
        elif text.startswith("/item add "):
            await self.app.transport.send(
                "character_edit", {"field": "add_item", "value": text[len("/item add "):].strip()}
            )
        elif text.startswith("/item remove "):
            await self.app.transport.send(
                "character_edit", {"field": "remove_item", "value": text[len("/item remove "):].strip()}
            )
        elif text.startswith("/equip "):
            await self.app.transport.send(
                "character_edit", {"field": "equip", "value": text[len("/equip "):].strip()}
            )
        elif text.startswith("/unequip "):
            await self.app.transport.send(
                "character_edit", {"field": "unequip", "value": text[len("/unequip "):].strip()}
            )
        elif text.startswith("/export"):
            filename = text[len("/export"):].strip() or "character"
            message = await self.app.export_character(filename)
            self.write_log(f"[dim]{message}[/dim]")
        elif text.startswith("/transcript"):
            # Client-side only, no protocol involved at all - the running
            # log this reads from is already everything the player has seen
            # this session, so there's nothing to ask the server for.
            filename = text[len("/transcript"):].strip() or "transcript"
            path = Path(filename)
            if path.suffix != ".txt":
                path = path.with_suffix(".txt")
            try:
                path.write_text(_transcript_text(self.query_one("#log", RichLog)))
            except OSError as exc:
                self.write_log(f"[dim]Couldn't save transcript: {exc}[/dim]")
            else:
                self.write_log(f"[dim]Transcript saved to {path}[/dim]")
        else:
            # Only a real player_action triggers a DM narrate() call - /roll
            # and /chat are handled instantly server-side with no LLM in the
            # loop, so there's nothing to wait on for those.
            self.set_thinking(True)
            await self.app.transport.send("player_action", {"text": text})


class DungeonMasterApp(App):
    """Holds session state as plain attributes (mirroring this workspace's
    own established TUI pattern for a multi-screen app - state lives on the
    App, each screen reads/writes self.app.* directly) and routes incoming
    envelopes to whichever screen is currently active. compose() is
    deliberately left as the base App's no-op: WelcomeScreen owns the
    entire first view, pushed from on_mount()."""

    def __init__(self, uri: str, player_id: str, is_new_character: bool):
        super().__init__()
        self._uri = uri
        self._player_id = player_id
        self.is_new_character = is_new_character
        self.transport: ClientTransport | None = None
        self.my_character: dict = {}
        self.others: dict[str, dict] = {}  # player_id -> public view, keyed the same way the server sends it
        self.world: dict = {}
        self.npcs: dict = {}
        self.log_tail: list[dict] = []
        self._listening = False

    @property
    def player_id(self) -> str:
        # Public, unlike _player_id above - WelcomeScreen's Solo mode needs
        # this to derive a stable per-client solo session_id (see _join()),
        # the same "screens read self.app.* directly" pattern this class's
        # own docstring already establishes for is_new_character/
        # my_character/etc.
        return self._player_id

    async def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())

    async def connect_and_join(
        self, player_name: str, session_id: str, character_class: str, imported_character: dict | None = None
    ) -> None:
        self.transport = ClientTransport(self._uri, session_id, self._player_id)
        await self.transport.connect()
        if not self._listening:
            self._listening = True
            asyncio.create_task(self._listen())
        payload = {"player_name": player_name, "character_class": character_class}
        if imported_character is not None:
            payload["imported_character"] = imported_character
        await self.transport.send("join_session", payload)

    async def export_character(self, filename: str) -> str:
        """Writes the current player's own full character sheet to a local
        JSON file - the client-side half of character save/load (the
        server-side half is join_session's optional imported_character
        field, see WelcomeScreen/_load_character_file above).
        self.my_character already carries everything worth saving (xp,
        level, inventory, notes, ...) since it's the exact private
        sheet_delta state_sync/character_update already deliver to their
        owner - no new protocol needed just to export it. Returns a
        user-facing status string rather than raising, matching this
        client's existing "report, don't crash the input loop" convention
        (WelcomeScreen's own OSError handling on connect)."""
        path = Path(filename)
        if path.suffix != ".json":
            path = path.with_suffix(".json")
        try:
            path.write_text(json.dumps(self.my_character, indent=2))
        except OSError as exc:
            return f"Couldn't export: {exc}"
        return f"Character exported to {path}"

    async def _listen(self) -> None:
        async for envelope in self.transport.messages():
            await self._handle(envelope)

    def _try_query_one(self, selector: str, expect_type: type):
        # self.screen, not self.query_one() - a real, non-obvious Textual
        # gotcha found by actually running this, not by reading docs:
        # App._on_compose pins App.query_one()'s search target
        # (default_screen/_compose_screen) to whatever screen existed the
        # moment App.compose() first ran, permanently - it never tracks
        # later push_screen()/switch_screen() calls. self.screen (top of
        # the real screen stack) is what actually reflects the currently
        # active screen.
        try:
            return self.screen.query_one(selector, expect_type)
        except NoMatches:
            return None

    def refresh_sheet_widgets(self) -> None:
        # Safe to call regardless of which screen is currently active or
        # whether it's even mounted yet - a no-op if #sheet doesn't exist
        # in the current screen (e.g. WelcomeScreen, which has none).
        sheet = self._try_query_one("#sheet", CharacterSheetPanel)
        if sheet is None:
            return
        sheet.render_sheet(self.my_character)
        sheet.render_others(self.others)
        sheet.render_world(self.world)
        sheet.render_npcs(self.npcs)

    def _roller_name(self, roller_id: str) -> str:
        # dice_result's roller_id is always a real player_id - both
        # dice_roll (player-initiated) and request_roll (DM-initiated,
        # engine.py's _narrate_and_apply) are always attributed to the
        # acting character, never an NPC.
        if roller_id == self._player_id:
            return self.my_character.get("name") or "You"
        return self.others.get(roller_id, {}).get("name", "Someone")

    def _dice_result_line(self, payload: dict) -> str:
        name = self._roller_name(payload.get("roller_id"))
        notation = payload.get("dice", "")
        total = payload.get("result")
        rolls = payload.get("rolls") or []
        sides = payload.get("sides")
        purpose = payload.get("purpose")
        label = f" ({purpose})" if purpose else ""

        # damage_type only ever appears on a DM-requested weapon damage
        # roll (server/engine.py's request_roll closure resolving a real
        # weapon field) - shown right after the die itself, matching the
        # server's own tool_result wording ("1d8 (slashing)").
        damage_type = payload.get("damage_type")
        if damage_type:
            notation = f"{notation} ({damage_type})"

        # ability/ability_modifier only ever appear on a DM-requested roll
        # tied to one of the character's own ability scores
        # (server/engine.py's request_roll closure) - shown as its own
        # "+N ABIL" tag so the total's makeup stays visible, not folded
        # invisibly into a total that otherwise wouldn't explain itself.
        ability_mod = payload.get("ability_modifier")
        if ability_mod is not None:
            sign = "+" if ability_mod >= 0 else ""
            notation = f"{notation} {sign}{ability_mod} {payload['ability'].upper()}"

        # skill only ever appears on a DM-requested roll that named one
        # (server/engine.py's request_roll closure) - shown with the real
        # skill name and, when proficient, the real proficiency bonus that
        # was actually added, the same "explain the roll, don't just show
        # a bare number" transparency every other tag here already follows.
        skill = payload.get("skill")
        if skill:
            skill_label = skill.replace("_", " ").title()
            if payload.get("proficient"):
                skill_label += f", +{payload['proficiency_bonus']} proficiency"
            notation = f"{notation} ({skill_label})"

        # spell only ever appears on a DM-requested attack-roll-shaped
        # spell (server/engine.py's request_roll closure resolving a real
        # "attack": true srd.json entry) - a spell attack always gets
        # proficiency in real 5e, unlike a skill check, so this tag always
        # includes it rather than conditionally like skill's does above.
        spell = payload.get("spell")
        if spell:
            notation = f"{notation} ({spell}, +{payload['proficiency_bonus']} proficiency)"

        # roll_kind only ever appears on a DM-requested roll that named one
        # (server/engine.py's request_roll closure) - purely descriptive of
        # what the roll represents (attack/save/check), shown the same way
        # damage_type/ability are so the roll reads as "what it actually
        # was", not just a bare number.
        roll_kind = payload.get("roll_kind")
        if roll_kind:
            notation = f"{notation} ({roll_kind})"

        # disadvantage_reasons only ever appears on a request_roll the
        # engine automatically rolled with disadvantage (server/engine.py's
        # _has_disadvantage, triggered by a tracked condition like
        # poisoned/frightened/prone) - names the real reason so it's not
        # just a bare, unexplained "why did that come out low".
        disadvantage_reasons = payload.get("disadvantage_reasons")
        if disadvantage_reasons:
            notation = f"{notation} (disadvantage: {', '.join(disadvantage_reasons)})"

        # Highlight a natural max (a "20" on a d20, but generalized to
        # whatever die was actually rolled) or a natural min the same way -
        # exactly the "highlighting a natural 20" example
        # docs/protocol.md's own known-gap note named as the payoff for
        # handling this envelope at all. With disadvantage, `rolls` holds
        # both d20s but only the *worse* one was actually kept - checking
        # either raw entry would wrongly highlight green off a high die
        # that got discarded, so the check narrows to the kept die when
        # disadvantage applied, while still displaying both rolls below.
        highlight_rolls = rolls
        if payload.get("disadvantage") and len(rolls) == 2:
            highlight_rolls = [min(rolls)]
        rolls_text = str(rolls)
        if sides:
            if any(r == sides for r in highlight_rolls):
                rolls_text = f"[b green]{rolls}[/b green]"
            elif any(r == 1 for r in highlight_rolls):
                rolls_text = f"[b red]{rolls}[/b red]"

        text = f"{name} rolls {notation}{label}: {total} {rolls_text}"
        if payload.get("dc") is not None:
            text += f" vs DC {payload['dc']}"
            success = payload.get("success")
            if success is not None:
                text += " — success" if success else " — failure"
        # critical only ever appears on a real natural-20 attack roll
        # (server/engine.py's request_roll closure) - a distinct callout,
        # not just relying on the green digit highlight above, since that
        # highlight already fires for any natural max on any roll (a skill
        # check's nat 20 is just a great check, not a critical hit).
        if payload.get("critical"):
            text += " [b red]CRITICAL HIT![/b red]"
        return text

    async def _handle(self, envelope: Envelope) -> None:
        # async, and _listen() awaits each call in turn (not fire-and-
        # forget) specifically so the push_screen/switch_screen awaits
        # below can matter - a real race, found only by running this
        # against a real server, not by reading the code: push_screen()
        # schedules mounting the new screen's widgets asynchronously, and
        # without awaiting it, the very next queued envelope (routinely
        # the "X joined the session" system_message broadcast, sent right
        # after state_sync in _on_join_session) could already be dispatched
        # and try to query a widget (e.g. #lobby-status) that doesn't exist
        # in the DOM yet, crashing with NoMatches.
        if envelope.type == "state_sync":
            payload = envelope.payload
            characters = payload.get("characters", {})
            self.my_character = characters.get(self._player_id, {})
            self.others = {pid: c for pid, c in characters.items() if pid != self._player_id}
            self.world = payload.get("world_state", {})
            self.npcs = payload.get("npcs", {})
            self.log_tail = payload.get("log_tail", [])

            if isinstance(self.screen, WelcomeScreen):
                # started: whether the adventure has already begun (see
                # GameEngine._has_started()) - a fresh join lands in the
                # lobby, a reconnect into an already-started game skips
                # straight to the real session view.
                if payload.get("started"):
                    await self.push_screen(SessionScreen())
                else:
                    await self.push_screen(LobbyScreen())
            else:
                self.refresh_sheet_widgets()
            return

        if envelope.type == "character_update":
            if envelope.payload.get("player_id") == self._player_id:
                self.my_character = envelope.payload.get("sheet_delta", {})
                self.refresh_sheet_widgets()
            return

        if envelope.type in ("player_joined", "player_update"):
            pid = envelope.payload.get("player_id")
            if pid and pid != self._player_id:
                self.others[pid] = envelope.payload
                self.refresh_sheet_widgets()
            return

        if envelope.type == "player_left":
            pid = envelope.payload.get("player_id")
            if self.others.pop(pid, None) is not None:
                self.refresh_sheet_widgets()
            return

        if envelope.type == "session_started":
            if isinstance(self.screen, LobbyScreen):
                await self.switch_screen(SessionScreen())
            return

        if envelope.type == "world_update":
            self.world = envelope.payload
            self.refresh_sheet_widgets()
            return

        # Everything below only ever matters once the real session view
        # exists - a lobby-phase chat_message is the one exception, handled
        # separately since it targets #chat-log, not #log.
        session_screen = self.screen if isinstance(self.screen, SessionScreen) else None

        if envelope.type == "npc_update":
            name = envelope.payload.get("name", "?")
            sheet_delta = envelope.payload.get("sheet_delta", {})
            # Keeps the persistent NPCs panel section current - self.npcs
            # was previously only ever set once, from state_sync, and never
            # updated by a live npc_update at all (a real staleness bug,
            # found while wiring the panel up to actually reflect this).
            self.npcs[name] = sheet_delta
            self.refresh_sheet_widgets()
            if session_screen is not None:
                # Still also a log line - a narrative "something happened"
                # beat distinct from the panel's always-current state,
                # the same "log entry announces, panel reflects" split
                # character_update/system_message already establish.
                session_screen.write_log(_npc_status_line(name, sheet_delta))
            return

        if envelope.type == "log_entry":
            text = envelope.payload.get("text", "")
            kind = envelope.payload.get("kind")
            if session_screen is not None and kind == "narration":
                session_screen.handle_narration_chunk(text, bool(envelope.payload.get("done")))
            elif session_screen is not None and kind == "dice":
                # Skipped here, not written - the structured dice_result
                # branch below renders the same roll with individual-die
                # highlighting the plain text can't carry. Both envelopes
                # broadcast for every roll (server/engine.py); rendering
                # both here would show each roll twice.
                pass
            elif session_screen is not None and kind == "outcome":
                # A direct owner ask: damage/heal/spell/item/condition
                # should read differently at a glance, not blend into
                # plain narration text. category (server/engine.py's
                # _outcome_category) picks the color; an unrecognized or
                # missing category still gets the line, just uncolored.
                color = _OUTCOME_COLORS.get(envelope.payload.get("category"))
                session_screen.write_log(f"[{color}]{text}[/{color}]" if color else text)
            elif session_screen is not None:
                session_screen.write_log(text)
            elif kind == "chat" and isinstance(self.screen, LobbyScreen):
                self.screen.query_one("#chat-log", RichLog).write(text)
            return

        if envelope.type == "dice_result":
            # Closes docs/protocol.md's own documented "known client gap" -
            # this used to have no handler at all, so a roll's only visible
            # trace was the plain log_entry text line skipped above. Built
            # from the structured payload instead so a natural max/min on
            # any individual die can actually be highlighted, not just
            # reproduced as the same flat text.
            if session_screen is not None:
                session_screen.write_log(self._dice_result_line(envelope.payload))
            return

        if envelope.type == "turn_prompt":
            # A real, previously-unexercised multiplayer gap, found only by
            # actually running two real clients through a real session
            # (see ROADMAP.md): turn_prompt broadcasts to everyone, but
            # this used to only ever write something for the player whose
            # turn it now is - anyone else got no indication at all of
            # whose turn it was, not even at session start. Every other
            # player's public view (name included) is already tracked in
            # self.others from player_joined/state_sync, so this needs no
            # new protocol field - just using data already on hand.
            if session_screen is not None:
                turn_player_id = envelope.payload.get("player_id")
                if turn_player_id == self._player_id:
                    session_screen.write_log("[i]Your turn.[/i]")
                else:
                    name = self.others.get(turn_player_id, {}).get("name", "Someone")
                    session_screen.write_log(f"[i]{name}'s turn.[/i]")
            return

        if envelope.type == "system_message":
            text = envelope.payload.get("text", "")
            # advisory (server/engine.py's missed-change heuristic) is a
            # genuinely different category from every other system_message -
            # a "you might want to double check" nudge, not a plain fact
            # about a connection/turn-order/save event - so it gets a
            # visually distinct treatment instead of blending in with the
            # rest at the same dim styling.
            rendered = f"[yellow]⚠ {text}[/yellow]" if envelope.payload.get("advisory") else f"[dim]{text}[/dim]"
            if session_screen is not None:
                session_screen.set_thinking(False)  # covers narration-failed, which never reaches a log_entry
                session_screen.write_log(rendered)
            elif isinstance(self.screen, LobbyScreen):
                self.screen.query_one("#lobby-status", Static).update(rendered)
            return
