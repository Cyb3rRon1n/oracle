# Companion NPC "Tinder" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Tinder, an optional DM-controlled comic-relief party companion, addable/dismissable by any player, built entirely on Oracle's existing `CharacterSheet`/NPC infrastructure.

**Architecture:** Tinder is a real `CharacterSheet` living in `Session.npcs`, marked `is_companion=True`, membership tracked by a new `Session.companion_joined` bool (not by adding/removing the NPC record itself — this codebase has no NPC-deletion mechanism, and reusing the existing "dead NPCs just stay in the dict" pattern is simpler than inventing one). Two new symmetric, idempotent client→server events (`add_companion`/`remove_companion`) mirror `start_combat`/`end_combat` exactly. Tinder's personality reaches the DM through a new prompt block appended to `world_summary` (the same channel `_npc_roster`/the fact ledger/lorebook already use) — no new tool call, no new LLM-facing schema field. Tinder never enters `turn_order`, never gets HP damage, never takes a scripted turn. Other players see Tinder exactly as they'd see a real teammate: a new `_npc_view()` redaction switch routes companion NPCs through the existing `_public_character_view()` (which already hides personality/ideals/bonds/flaws/notes/inventory) instead of a full `model_dump()`, which is what every *other* NPC still gets unchanged.

**Tech Stack:** Python 3.13 / Pydantic (server), React (web client), pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-09-13-companion-npc-design.md`

## Global Constraints

- No HP, no combat participation, no action economy, no turn, no `player_action` for Tinder — it must never appear in `turn_order` and must never be rolled into a `start_combat` initiative announcement.
- One companion only (`Tinder`) — no multi-persona roster, no per-party customization.
- No change to `server/lore/isekai.json`'s `tone_guidance` — Tinder is a deliberate tonal contrast, not a world-tone change.
- Other players' view of Tinder must match what they'd see of a real teammate (`_public_character_view`'s existing redaction), never a full sheet dump.
- No new server→client `EventType` — companion state changes ride the existing `npc_update`/`state_sync` envelopes with one new optional field each, exactly like `range_band`/`disposition` were added to those payloads in the prior BG3-backlog arc.
- Piece A (creation-time personality control in the join flow) and Piece C (the `PollinationsImageBackend`, already shipped separately) are explicitly **not** part of this plan.
- `shared/protocol.py`'s `EventType` and `web/src/lib/protocol.js`'s `EventType` must stay in sync — `tests/test_protocol_sync.py` fails CI otherwise.

---

### Task 1: Protocol events and state fields (foundation)

**Files:**
- Modify: `shared/protocol.py` (EventType Literal)
- Modify: `web/src/lib/protocol.js` (EventType object)
- Modify: `server/state.py` (`CharacterSheet`, `Session`)
- Test: `tests/test_protocol_sync.py` (existing, no changes needed — just must keep passing)
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `CharacterSheet.is_companion: bool` (default `False`), `Session.companion_joined: bool` (default `False`), `Session.turns_since_world_change: int` (default `0`), and the `"add_companion"`/`"remove_companion"` event-type strings on both sides.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_state.py`:

```python
def test_character_sheet_is_companion_defaults_false():
    sheet = CharacterSheet(player_id="p1", name="Thrain", hp=10, max_hp=10)
    assert sheet.is_companion is False


def test_session_companion_fields_default():
    session = Session(session_id="s1")
    assert session.companion_joined is False
    assert session.turns_since_world_change == 0
```

(Check the top of `tests/test_state.py` for its existing `CharacterSheet`/`Session` imports — reuse them, don't re-import.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_state.py -k "companion" -v`
Expected: FAIL — `AttributeError: 'CharacterSheet' object has no attribute 'is_companion'` (and the `Session` equivalent).

- [ ] **Step 3: Add the fields**

In `server/state.py`, add to `CharacterSheet` right after the `range_band` field (around line 178, keeping the same "shared-model tradeoff" comment style as `disposition`/`range_band`):

```python
    # The party's optional comic-relief companion (docs/protocol.md
    # "Companion NPC") - same shared-model tradeoff as disposition/range_band:
    # meaningful for the one NPC it's ever true on, unused on a player
    # character or any other tracked NPC. Gates: exclusion from
    # _on_start_combat's initiative roll, the _npc_view() visibility
    # redaction, and the _npc_roster() DM status line.
    is_companion: bool = False
```

In `server/state.py`, add to `Session` near `turns_since_summary` (search for that field, add right after it):

```python
    # Whether Tinder (docs/protocol.md "Companion NPC") is currently with
    # the party. The companion's own CharacterSheet lives in `npcs` and is
    # never deleted once created (this codebase has no NPC-deletion
    # mechanism - dead monster NPCs already just stay in the dict); this
    # bool, not presence in `npcs`, is the real membership source of truth.
    companion_joined: bool = False
    # Turns since GameEngine's own `world_changed` flag was last true (i.e.
    # since update_world last actually changed something) - not reset on a
    # fixed schedule like turns_since_summary. Feeds the companion's pacing
    # nudge (see _companion_prompt_block); 0 means the world just changed.
    turns_since_world_change: int = 0
```

- [ ] **Step 4: Run the new tests, verify they pass**

Run: `uv run --extra dev python3 -m pytest tests/test_state.py -k "companion" -v`
Expected: PASS

- [ ] **Step 5: Add the protocol event types**

In `shared/protocol.py`, add to the `EventType` Literal right after `"end_combat",`:

```python
    "add_companion",
    "remove_companion",
```

In `web/src/lib/protocol.js`, add to the `EventType` object right after `END_COMBAT: "end_combat",`:

```js
  ADD_COMPANION: "add_companion",
  REMOVE_COMPANION: "remove_companion",
```

- [ ] **Step 6: Run the protocol sync test and the full state test file**

Run: `uv run --extra dev python3 -m pytest tests/test_protocol_sync.py tests/test_state.py -v`
Expected: PASS (both files)

- [ ] **Step 7: Commit**

```bash
git add shared/protocol.py web/src/lib/protocol.js server/state.py tests/test_state.py
git commit -m "feat: add companion protocol events and session/sheet fields"
```

---

### Task 2: Tinder's preset sheet

**Files:**
- Modify: `server/character_build.py`
- Test: `tests/test_engine.py` (imports re-exported names from `server.engine`, matching this file's existing convention)

**Interfaces:**
- Consumes: `CharacterSheet` (`server/state.py`), `STARTING_HP` (`server/character_build.py`, already defined as `100`).
- Produces: `COMPANION_KEY: str` (`"tinder"`), `build_companion_sheet() -> CharacterSheet`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engine.py` (near the other `build_starting_character` tests):

```python
def test_build_companion_sheet_returns_tinders_preset():
    companion = build_companion_sheet()

    assert companion.name == "Tinder"
    assert companion.is_companion is True
    assert companion.hp == companion.max_hp == STARTING_HP
    assert companion.personality
    assert companion.ideals
    assert companion.bonds
    assert companion.flaws
```

Add `build_companion_sheet` and `STARTING_HP` to the existing `from server.engine import (...)` block at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k test_build_companion_sheet -v`
Expected: FAIL — `ImportError: cannot import name 'build_companion_sheet'`

- [ ] **Step 3: Implement**

In `server/character_build.py`, add after `STARTING_HP = 100`:

```python
# The party's optional comic-relief companion (docs/protocol.md "Companion
# NPC"). Dict key for Session.npcs - casefolded like every other NPC key
# (build_starting_character's own npc_key convention in server/engine.py),
# not the display-cased name.
COMPANION_KEY = "tinder"


def build_companion_sheet() -> CharacterSheet:
    """Tinder's fixed preset sheet - one companion, not a roster (see the
    design spec). No HP/combat participation is enforced at the engine
    layer (never rolled into initiative, never damaged), not by this sheet
    alone; hp/max_hp are set to the same flat STARTING_HP every player gets
    purely so shared view code (_public_character_view etc.) never needs a
    companion-specific branch for a field every CharacterSheet already has.

    Personality is motivated by Aetherfall's own central mechanic, not
    bolted on: crossing the Veil costs everyone something real, chosen by
    the Veil, never by the person. Tinder's toll was their sense of fear/
    self-preservation - the in-fiction reason they're funny and, rarely,
    genuinely reckless (see docs/protocol.md "Companion NPC" and the design
    spec's "Identity" section)."""
    return CharacterSheet(
        player_id=COMPANION_KEY,
        name="Tinder",
        hp=STARTING_HP,
        max_hp=STARTING_HP,
        is_companion=True,
        personality=(
            "Grins in the face of things that should terrify anyone else - not bravado, "
            "just genuinely can't remember what being scared felt like. Fills silence with "
            "jokes, mostly to avoid noticing how much quieter everything got."
        ),
        ideals="\"Nobody else has to lose what I lost - not on my watch.\" (Freedom)",
        bonds="Owes the Veil nothing and everyone at this table everything, whether they asked for the debt or not.",
        flaws="Leaps first, thinks never - or at least not until it's too late to matter.",
    )
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k test_build_companion_sheet -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/character_build.py tests/test_engine.py
git commit -m "feat: add Tinder's preset companion sheet"
```

---

### Task 3: Companion visibility redaction (`_npc_view`)

**Files:**
- Modify: `server/views.py`
- Modify: `server/engine.py` (`_state_sync_envelope`, `_npc_update_envelope`)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `_public_character_view(character) -> dict` (existing, `server/views.py`), `CharacterSheet.is_companion` (Task 1).
- Produces: `_npc_view(npc: CharacterSheet) -> dict`. `_npc_update_envelope` gains a keyword-only `joined: bool | None = None` param (used starting Task 4; harmless/unused until then).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py`:

```python
def test_npc_view_redacts_a_companion_like_a_public_character():
    companion = build_companion_sheet()
    companion.notes = "secret DM-only note"

    view = _npc_view(companion)

    assert view == _public_character_view(companion)
    assert "notes" not in view
    assert "personality" not in view


def test_npc_view_returns_the_full_sheet_for_an_ordinary_npc():
    npc = CharacterSheet(player_id="goblin", name="goblin", hp=7, max_hp=7, notes="a real note")

    view = _npc_view(npc)

    assert view == npc.model_dump()
    assert view["notes"] == "a real note"
```

Add `_npc_view` and `CharacterSheet` (if not already imported) to the test file's import blocks.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k test_npc_view -v`
Expected: FAIL — `ImportError: cannot import name '_npc_view'`

- [ ] **Step 3: Implement `_npc_view` in `server/views.py`**

Add right after `_public_character_view`'s definition:

```python
def _npc_view(npc: CharacterSheet) -> dict:
    """What's broadcast for one tracked NPC. An ordinary NPC/monster has no
    private owner, so it's always been broadcast in full (docs/protocol.md
    "Private vs. shared state") - unchanged here. The party's companion
    (CharacterSheet.is_companion) is the one exception: it should feel like
    another player, not an open monster sheet, so it gets the same
    redacted _public_character_view every real player already gets of
    every other player - the same owner-only boundary (personality/notes/
    inventory/stats hidden), just extended to cover this one NPC."""
    return _public_character_view(npc) if npc.is_companion else npc.model_dump()
```

- [ ] **Step 4: Wire it into the two broadcast sites in `server/engine.py`**

In the `from .views import (...)` block, add `_npc_view` alongside the existing names.

Change `_state_sync_envelope`'s `"npcs"` line from:

```python
                "npcs": {npc.name: npc.model_dump() for npc in self._session.npcs.values()},
```

to:

```python
                "npcs": {npc.name: _npc_view(npc) for npc in self._session.npcs.values()},
```

Change `_npc_update_envelope` from:

```python
    def _npc_update_envelope(self, name: str, npc: CharacterSheet) -> Envelope:
        # Broadcast - an NPC's wounds/conditions are shared observable fiction.
        return Envelope(
            type="npc_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload={"name": name, "sheet_delta": npc.model_dump()},
        )
```

to:

```python
    def _npc_update_envelope(self, name: str, npc: CharacterSheet, *, joined: bool | None = None) -> Envelope:
        # Broadcast - an NPC's wounds/conditions are shared observable fiction.
        # sheet_delta goes through _npc_view so the party companion gets the
        # same redaction a real teammate would (see _npc_view's own comment);
        # every other NPC is unaffected, same full dump as before. `joined`
        # is None (omitted from the payload shape entirely, existing
        # behavior) for every call except the companion add/remove path
        # (Task 4), which sets it explicitly.
        payload: dict = {"name": name, "sheet_delta": _npc_view(npc)}
        if joined is not None:
            payload["joined"] = joined
        return Envelope(
            type="npc_update",
            session_id=self._session.session_id,
            sender_id="server",
            payload=payload,
        )
```

- [ ] **Step 5: Run the new tests and the full existing NPC-related test suite**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "npc_view or npc_update or state_sync" -v`
Expected: PASS — including every pre-existing `npc_update`/`state_sync` test (they assert on full `model_dump()`-shaped data for ordinary NPCs, which is unchanged).

- [ ] **Step 6: Run the full test suite to catch any other caller of the old two-arg `_npc_update_envelope` signature**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures (the added `joined` parameter is keyword-only with a default, so every existing two-positional-arg call site is unaffected).

- [ ] **Step 7: Commit**

```bash
git add server/views.py server/engine.py tests/test_engine.py
git commit -m "feat: redact the companion NPC's broadcast view like a real player"
```

---

### Task 4: `add_companion` / `remove_companion` handlers

**Files:**
- Modify: `server/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `COMPANION_KEY`, `build_companion_sheet` (Task 2), `_npc_update_envelope(..., joined=...)` (Task 3), `Session.companion_joined` (Task 1), `self._save()` (existing).
- Produces: `GameEngine._on_add_companion`, `GameEngine._on_remove_companion` (dispatched automatically by `handle()`'s `_on_{envelope.type}` convention — no dispatch-table change needed).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py` (near the `_start_combat`/`_end_combat` helpers and tests):

```python
async def _add_companion(engine, player_id):
    await engine.handle(Envelope(type="add_companion", session_id="test-session", sender_id=player_id, payload={}))


async def _remove_companion(engine, player_id):
    await engine.handle(Envelope(type="remove_companion", session_id="test-session", sender_id=player_id, payload={}))


async def test_add_companion_joins_tinder_and_broadcasts():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await _add_companion(engine, player_id)

    assert session.companion_joined is True
    assert COMPANION_KEY in session.npcs
    assert session.npcs[COMPANION_KEY].name == "Tinder"
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert updates[-1][2]["joined"] is True
    assert updates[-1][2]["name"] == "Tinder"


async def test_add_companion_is_idempotent():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await _add_companion(engine, player_id)
    before = len(received)
    await _add_companion(engine, player_id)

    assert len(received) == before  # no second broadcast


async def test_remove_companion_leaves_and_broadcasts():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await _add_companion(engine, player_id)

    await _remove_companion(engine, player_id)

    assert session.companion_joined is False
    updates = [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]
    assert updates[-1][2]["joined"] is False


async def test_remove_companion_is_idempotent_when_never_joined():
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)

    await _remove_companion(engine, player_id)

    assert session.companion_joined is False
    assert not [r for r in received if r[0] == "broadcast" and r[1] == "npc_update"]


async def test_add_companion_is_exempt_from_turn_order():
    # Same shape as test_start_combat_is_exempt_from_turn_order: any joined
    # player may send this regardless of whose turn it is.
    engine, session, _ = make_engine(StubDM())
    p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
    await join(engine, p1)
    await join(engine, p2)
    assert session.current_turn == p1

    await _add_companion(engine, p2)

    assert session.companion_joined is True
```

Add `COMPANION_KEY` to the file's `from server.engine import (...)` block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "add_companion or remove_companion" -v`
Expected: FAIL — handlers don't exist yet, so `handle()` silently no-ops (`getattr(self, f"_on_{envelope.type}", None)` returns `None`) and `session.companion_joined` stays `False`.

- [ ] **Step 3: Implement the handlers**

In `server/engine.py`, add right after `_on_end_combat` (so the two pairs of symmetric toggle handlers sit together):

```python
    async def _on_add_companion(self, envelope: Envelope) -> None:
        """Tinder joins the party (docs/protocol.md "Companion NPC") - any
        joined player may trigger this, same symmetric no-host-role shape as
        start_combat/tavern_rest. Idempotent: a second add_companion while
        already joined is a no-op. The sheet is created once and never
        deleted (see Session.companion_joined's own comment) - a later
        remove/add cycle reuses the same CharacterSheet instance, including
        whatever player_id-visible history it's picked up, rather than
        resetting Tinder to a blank slate each time."""
        if self._session.companion_joined:
            return
        companion = self._session.npcs.get(COMPANION_KEY)
        if companion is None:
            companion = build_companion_sheet()
            self._session.npcs[COMPANION_KEY] = companion
        self._session.companion_joined = True
        await self._broadcast(self._npc_update_envelope(companion.name, companion, joined=True))
        await self._broadcast(self._system_envelope(f"{companion.name} joins the party.", level="info"))
        await self._save()

    async def _on_remove_companion(self, envelope: Envelope) -> None:
        """Tinder leaves the party. Idempotent: a no-op if not currently
        joined. The sheet stays in session.npcs (not deleted - this
        codebase has no NPC-deletion mechanism); companion_joined is the
        real membership flag other logic (initiative, the DM prompt block)
        checks, not presence in npcs."""
        if not self._session.companion_joined:
            return
        companion = self._session.npcs[COMPANION_KEY]
        self._session.companion_joined = False
        await self._broadcast(self._npc_update_envelope(companion.name, companion, joined=False))
        await self._broadcast(
            self._system_envelope(f"{companion.name} steps back and leaves the party for now.", level="info")
        )
        await self._save()
```

Add `COMPANION_KEY` and `build_companion_sheet` to the existing `from .character_build import (...)` block at the top of `server/engine.py`.

- [ ] **Step 4: Run the new tests, verify they pass**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "add_companion or remove_companion" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures

- [ ] **Step 6: Commit**

```bash
git add server/engine.py tests/test_engine.py
git commit -m "feat: add add_companion/remove_companion handlers"
```

---

### Task 5: Exclude the companion from initiative and the NPC status roster

**Files:**
- Modify: `server/engine.py` (`_on_start_combat`)
- Modify: `server/views.py` (`_npc_roster`)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `CharacterSheet.is_companion` (Task 1).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py`:

```python
async def test_start_combat_excludes_the_companion_from_the_announced_roll_too():
    # Stricter than the existing ordinary-NPC exclusion test: Tinder isn't
    # even named in the initiative announcement, unlike a real monster NPC
    # (which is named but still excluded from turn_order - see
    # test_start_combat_announces_npcs_but_excludes_them_from_turn_order).
    engine, session, received = make_engine(StubDM())
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await _add_companion(engine, player_id)

    with patch("server.dice.random.randint", return_value=10):
        await _start_combat(engine, player_id)

    assert session.turn_order == [player_id]
    announcements = [
        r for r in received if r[0] == "broadcast" and r[1] == "system_message" and "Initiative" in r[2]["text"]
    ]
    assert "Tinder" not in announcements[0][2]["text"]


def test_npc_roster_excludes_the_companion():
    companion = build_companion_sheet()
    session = Session(session_id="s1")
    session.npcs[COMPANION_KEY] = companion

    assert _npc_roster(session) == ""
```

Add `_npc_roster` and `Session` to the test file's imports if not already present (both likely already are, per the file's existing `test_narrate_world_summary_includes_the_tracked_npc_roster` test).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "excludes_the_companion" -v`
Expected: FAIL — Tinder currently gets rolled/announced like any NPC, and shows up in `_npc_roster`'s output (`"Tracked NPCs:\n- Tinder: HP 100/100"`).

- [ ] **Step 3: Implement**

In `server/engine.py`'s `_on_start_combat`, change:

```python
        for npc in self._session.npcs.values():
            dex_mod = npc.stat_modifiers.get("dex", 0)
            total, _, _ = dice.roll("1d20", extra_modifier=dex_mod)
            participants.append((npc.name, total, dex_mod, None))
```

to:

```python
        for npc in self._session.npcs.values():
            if npc.is_companion:
                continue  # never rolled or announced - see docs/protocol.md "Companion NPC"
            dex_mod = npc.stat_modifiers.get("dex", 0)
            total, _, _ = dice.roll("1d20", extra_modifier=dex_mod)
            participants.append((npc.name, total, dex_mod, None))
```

In `server/views.py`'s `_npc_roster`, change:

```python
    for npc in session.npcs.values():
        if npc.hp <= 0:
            continue
```

to:

```python
    for npc in session.npcs.values():
        if npc.hp <= 0 or npc.is_companion:
            continue
```

- [ ] **Step 4: Run the new tests, verify they pass**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "excludes_the_companion" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures

- [ ] **Step 6: Commit**

```bash
git add server/engine.py server/views.py tests/test_engine.py
git commit -m "feat: exclude the companion from initiative and the NPC roster"
```

---

### Task 6: `turns_since_world_change` counter

**Files:**
- Modify: `server/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `world_changed` (existing local variable in the turn-resolution method), `Session.turns_since_world_change` (Task 1).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py`:

```python
async def test_turns_since_world_change_resets_when_the_world_changes():
    dm = UpdateSequenceDM([{"location": "Millbrook"}])
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    session.turns_since_world_change = 3

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I arrive"},
    ))

    assert session.turns_since_world_change == 0


async def test_turns_since_world_change_increments_when_nothing_changes():
    dm = StubDM()  # never calls update_world
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    assert session.turns_since_world_change == 0

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert session.turns_since_world_change == 1
```

Check `UpdateSequenceDM`'s existing constructor shape (`tests/test_engine.py`, already used by several tests above) — pass an `update_world`-shaped dict the same way `test_narrate_world_summary_includes_the_tracked_npc_roster` does, adjusting the key name if `UpdateSequenceDM` expects a different wrapping than `update_character`'s (check its `narrate()` body for how it decides which callback to invoke).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k test_turns_since_world_change -v`
Expected: FAIL — `session.turns_since_world_change` stays `0` regardless (the field exists from Task 1 but nothing writes to it yet).

- [ ] **Step 3: Implement**

In `server/engine.py`, find:

```python
        if world_changed:
            await self._broadcast(self._world_update_envelope())
```

and change it to:

```python
        if world_changed:
            self._session.turns_since_world_change = 0
            await self._broadcast(self._world_update_envelope())
        else:
            self._session.turns_since_world_change += 1
```

- [ ] **Step 4: Run the new tests, verify they pass**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k test_turns_since_world_change -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures

- [ ] **Step 6: Commit**

```bash
git add server/engine.py tests/test_engine.py
git commit -m "feat: track turns since the world last meaningfully changed"
```

---

### Task 7: Companion prompt block (reactive banter, chaos beat, pacing nudge)

**Files:**
- Modify: `server/views.py`
- Modify: `server/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Session.companion_joined`, `Session.npcs`, `Session.turns_since_world_change` (Task 1/6).
- Produces: `_companion_prompt_block(session: Session) -> str`; injected into the same `world_summary` string `narrate()` already receives.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py`:

```python
def test_companion_prompt_block_is_empty_when_not_joined():
    session = Session(session_id="s1")
    assert _companion_prompt_block(session) == ""


def test_companion_prompt_block_includes_personality_when_joined():
    session = Session(session_id="s1")
    session.npcs[COMPANION_KEY] = build_companion_sheet()
    session.companion_joined = True

    block = _companion_prompt_block(session)

    assert "Tinder" in block
    assert "no HP" in block
    assert build_companion_sheet().personality in block


def test_companion_prompt_block_adds_a_pacing_nudge_when_the_world_is_stale():
    session = Session(session_id="s1")
    session.npcs[COMPANION_KEY] = build_companion_sheet()
    session.companion_joined = True
    session.turns_since_world_change = 6

    block = _companion_prompt_block(session)

    assert "6 turns" in block
    assert "progress" in block


def test_companion_prompt_block_has_no_pacing_nudge_when_the_world_is_fresh():
    session = Session(session_id="s1")
    session.npcs[COMPANION_KEY] = build_companion_sheet()
    session.companion_joined = True
    session.turns_since_world_change = 1

    block = _companion_prompt_block(session)

    assert "progress" not in block


async def test_narrate_world_summary_includes_the_companion_block_when_joined():
    dm = UpdateSequenceDM([{}])  # no-op update, just need a narrate() call
    engine, session, _ = make_engine(dm)
    player_id = str(uuid.uuid4())
    await join(engine, player_id)
    await _add_companion(engine, player_id)
    engine._dm = recorder = OpeningSceneDM()

    await engine.handle(Envelope(
        type="player_action", session_id="test-session", sender_id=player_id,
        payload={"text": "I look around"},
    ))

    assert "Tinder" in recorder.world_summaries[-1]
```

(`OpeningSceneDM` is the existing `world_summaries`-recording DM double already used by `test_narrate_world_summary_includes_the_tracked_npc_roster` — reuse it, don't redefine it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "companion_prompt_block or companion_block" -v`
Expected: FAIL — `ImportError: cannot import name '_companion_prompt_block'`

- [ ] **Step 3: Implement `_companion_prompt_block` in `server/views.py`**

Add a module constant near the top (after `NPC_NOTES_CONTEXT_MAX_CHARS`) and the function near `_npc_roster`:

```python
# First-guess threshold, not yet tuned against real play - the design spec
# deliberately left this untimed/uncalibrated pending the live playtest
# (docs/superpowers/specs/2026-09-13-companion-npc-design.md "Mechanics").
COMPANION_STALE_WORLD_TURNS = 6


def _companion_prompt_block(session: Session) -> str:
    """Reaches the DM through world_summary, the same channel _npc_roster/
    the fact ledger/lorebook already ride (server/engine.py's per-turn
    narrate() call) - no new tool call, no new LLM-facing schema field.
    Empty when Tinder isn't currently with the party, the same "don't
    render the absent default" convention _npc_roster's own empty-string
    return already follows."""
    if not session.companion_joined:
        return ""
    companion = session.npcs.get(COMPANION_KEY)
    if companion is None:
        return ""
    lines = [
        f"{companion.name} is with the party as an optional comic-relief companion - "
        "no HP, never in combat, never takes a turn, never acts through a tool call.",
        f"Personality: {companion.personality}",
    ]
    if companion.ideals:
        lines.append(f"Ideals: {companion.ideals}")
    if companion.bonds:
        lines.append(f"Bonds: {companion.bonds}")
    if companion.flaws:
        lines.append(f"Flaws: {companion.flaws}")
    lines.append(
        f"Weave in a quick in-character reaction from {companion.name} when it fits naturally. "
        "Rarely - not most turns - if the moment calls for levity or the party's plan could use a "
        "shake-up, have them do something impulsive with a real narrative consequence, expressed "
        "only through your normal narration and tools, never a new mechanic."
    )
    if session.turns_since_world_change >= COMPANION_STALE_WORLD_TURNS:
        lines.append(
            f"It's been {session.turns_since_world_change} turns since anything in the world "
            f"genuinely moved forward - consider having {companion.name} (or the scene itself) "
            "nudge the party toward some kind of progress."
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Wire it into `server/engine.py`'s world_summary construction**

Add `_companion_prompt_block` to the `from .views import (...)` block.

Find:

```python
        npc_roster = _npc_roster(self._session)
        if npc_roster:
            world_summary = f"{world_summary}\n{npc_roster}" if world_summary else npc_roster
```

and add right after it:

```python
        companion_block = _companion_prompt_block(self._session)
        if companion_block:
            world_summary = f"{world_summary}\n\n{companion_block}" if world_summary else companion_block
```

- [ ] **Step 5: Run the new tests, verify they pass**

Run: `uv run --extra dev python3 -m pytest tests/test_engine.py -k "companion_prompt_block or companion_block" -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures

- [ ] **Step 7: Commit**

```bash
git add server/views.py server/engine.py tests/test_engine.py
git commit -m "feat: feed the companion's personality and pacing nudge to the DM prompt"
```

---

### Task 8: Client state (store.jsx)

**Files:**
- Modify: `web/src/state/store.jsx`

**Interfaces:**
- Consumes: `ET.ADD_COMPANION`, `ET.REMOVE_COMPANION` (Task 1), the `joined` field on `npc_update` payloads and `companion_joined` on `state_sync` payloads (Task 3/4).
- Produces: `actions.addCompanion()`, `actions.removeCompanion()`; `state.companionJoined: boolean`.

This task has no server-side component. `web/package.json` has no `test` script (only `dev`/`build`/`preview`/`lint`) and this repo has no existing JS test suite — introducing one for a single small reducer change isn't warranted, so this task is exercised by the `npm run build` compile check below and Task 9's manual live verification, not a JS unit test.

- [ ] **Step 1: Add the two actions**

In `web/src/state/store.jsx`, add right after the existing `endCombat()` action:

```js
      addCompanion() {
        connRef.current?.sendEvent(ET.ADD_COMPANION, {});
      },
      removeCompanion() {
        connRef.current?.sendEvent(ET.REMOVE_COMPANION, {});
      },
```

- [ ] **Step 2: Thread `companion_joined` through `STATE_SYNC`**

Find the `case ET.STATE_SYNC:` reducer block (near the top of the reducer, per the file's own `case ET.STATE_SYNC: {` at line 67) and read its full body first — it builds a fresh `state` object from `event.payload`. Add `companionJoined: event.payload.companion_joined ?? false,` alongside wherever it sets `inCombat`/`world` from the same payload, matching that block's existing field-naming convention (`event.payload.in_combat` → `state.inCombat`, so `event.payload.companion_joined` → `state.companionJoined`).

- [ ] **Step 3: Thread `joined` through `NPC_UPDATE`**

Change:

```js
    case ET.NPC_UPDATE:
      // event.payload is the envelope shape {name, sheet_delta} - merge the
      // delta's fields, not the wrapper itself, or every live NPC field
      // (hp, range_band, disposition, ...) ends up nested under a stray
      // `sheet_delta` key instead of reaching state.npcs[name] directly.
      return {
        ...state,
        npcs: { ...state.npcs, [event.payload.name]: { ...state.npcs[event.payload.name], ...event.payload.sheet_delta } },
      };
```

to:

```js
    case ET.NPC_UPDATE: {
      // event.payload is the envelope shape {name, sheet_delta} - merge the
      // delta's fields, not the wrapper itself, or every live NPC field
      // (hp, range_band, disposition, ...) ends up nested under a stray
      // `sheet_delta` key instead of reaching state.npcs[name] directly.
      // `joined` (companion add/remove only - see server/engine.py's
      // _npc_update_envelope) additionally flips state.companionJoined;
      // every ordinary NPC update omits it and leaves companionJoined alone.
      const next = {
        ...state,
        npcs: { ...state.npcs, [event.payload.name]: { ...state.npcs[event.payload.name], ...event.payload.sheet_delta } },
      };
      if (event.payload.joined !== undefined) next.companionJoined = event.payload.joined;
      return next;
    }
```

- [ ] **Step 4: Verify the build still compiles**

Run: `cd ~/projects/oracle/web && npm run build 2>&1 | tail -30`
Expected: build succeeds, no syntax errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/state/store.jsx
git commit -m "feat: add companion actions and reducer wiring"
```

---

### Task 9: Client UI toggle

**Files:**
- Modify: `web/src/components/GameScreen.jsx`

**Interfaces:**
- Consumes: `actions.addCompanion()`, `actions.removeCompanion()`, `state.companionJoined` (Task 8).

- [ ] **Step 1: Add the header toggle button**

In `web/src/components/GameScreen.jsx`, add right after the existing ⚔ combat toggle button (same `header` flex row):

```jsx
          <button
            className={`px-2 py-1 rounded border transition text-xs ${
              state.companionJoined ? "border-dungeon-gold text-dungeon-gold" : "border-dungeon-edge hover:border-dungeon-gold"
            }`}
            onClick={() => (state.companionJoined ? actions.removeCompanion() : actions.addCompanion())}
          >
            🎭 {state.companionJoined ? t("Dismiss Tinder") : t("Add Tinder")}
          </button>
```

- [ ] **Step 2: Verify the build still compiles**

Run: `cd ~/projects/oracle/web && npm run build 2>&1 | tail -30`
Expected: build succeeds.

- [ ] **Step 3: Manual live verification**

This is a real UI change — per this project's own verification culture (see `feedback_oracle_workflow.md`: "wants real live verification, not just passing tests"), start the dev server and client, join a session, click "Add Tinder", confirm the button flips to "Dismiss Tinder" and a system message announces Tinder joining, then click it again and confirm the reverse. This can be folded into Task 10's live playtest rather than done twice.

- [ ] **Step 4: Commit**

```bash
git add web/src/components/GameScreen.jsx
git commit -m "feat: add the Tinder join/dismiss toggle button"
```

---

### Task 10: Docs, CHANGELOG, and the live playtest

**Files:**
- Modify: `docs/protocol.md`
- Modify: `CHANGELOG.md`

**Interfaces:** None (documentation + manual verification only).

- [ ] **Step 1: Add a "Companion NPC" section to `docs/protocol.md`**

Add a new `## Companion NPC` section (after `## Shove`, before `## Interactive points of interest` — keeping the BG3-backlog-adjacent sections grouped, or at the end of the file if a reviewer prefers new work appended rather than interleaved; either is consistent with this file's existing mixed ordering). Cover, in this file's own established style (see `## NPC range band`/`## Shove` for the shape to match):

- What Tinder is and the Veil-took-their-fear motivation (one line, linking to the design spec for the full reasoning).
- `add_companion`/`remove_companion` (client→server, `{}`, idempotent, any joined player, same symmetric shape as `start_combat`/`end_combat`).
- `npc_update` gains an optional `joined: bool` field (companion add/remove only); `state_sync` gains `companion_joined: bool`.
- The `_npc_view` redaction: Tinder is the one NPC broadcast through `_public_character_view` instead of a full `model_dump()` — link this into the existing "Private vs. shared state" section's own list of redaction rules (that section should get a short addendum paragraph, the same way it already has three "joined this list" addenda for `class_features`/`skill_proficiencies`/`racial_traits`).
- Explicitly not built: no HP/combat/turn for the companion, no multi-persona roster, no player-editable companion personality.

Also add the two new rows to the `## Client → Server` table (matching the existing `start_combat`/`end_combat` row style exactly).

- [ ] **Step 2: Add the CHANGELOG entry**

Add to `CHANGELOG.md`, top of the `## v2 rebuild and after (2026-08)` section, matching this file's existing one-paragraph-per-entry style (see the `PollinationsImageBackend` or `Long-session world.summary cadence` entries immediately above it for the exact tone/density to match): summarize what shipped (Tinder, the add/remove toggle, the reactive-banter/chaos-beat/pacing-nudge prompt block, the visibility redaction), link to `docs/superpowers/specs/2026-09-13-companion-npc-design.md`, and note the date.

- [ ] **Step 3: Run the full test suite one final time**

Run: `uv run --extra dev python3 -m pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 4: Live playtest**

Per the design spec's own "Testing plan" and this session's earlier agreement — not a new scored script, a real played session. Start the server and client (`uv run python -m server.main`, `cd web && npm run dev`, or however this project's own `run` skill/README directs), join, click "Add Tinder", and play several turns covering: an ordinary narrative turn (does Tinder's banter show up naturally in the DM's prose), a turn engineered to sit idle for `COMPANION_STALE_WORLD_TURNS`+ turns (does the pacing nudge visibly change the DM's behavior), and a dismiss/rejoin mid-session (does the toggle and the system-message announcement both work cleanly). Report back qualitative notes — does the banter feel stale, does the chaos beat land or annoy, is the pacing threshold too eager or too quiet — the same deliverable format the spec commits to, not a pass/fail number.

- [ ] **Step 5: Commit**

```bash
git add docs/protocol.md CHANGELOG.md
git commit -m "docs: document the Tinder companion NPC feature"
```
