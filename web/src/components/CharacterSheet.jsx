// Tabbed character sheet - the owner's full view (state.character is the
// owner-only payload from state_sync/character_update: inventory, stats,
// spells, class features, proficiencies). Bookkeeping edits ride
// character_edit; everything else here is read-only projection.

import { useState } from "react";
import MapPanel from "./MapPanel.jsx";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

const ABILITY_LABELS = {
  str: "STR",
  dex: "DEX",
  con: "CON",
  int: "INT",
  wis: "WIS",
  cha: "CHA",
};

const TABS = ["Overview", "Map", "Abilities", "Inventory", "Spells", "Notes"];

export default function CharacterSheet() {
  const [tab, setTab] = useState("Overview");
  const { state } = useStore();
  const { t } = useLang();
  const tr = (s) => t(s);
  const sheet = state.character;

  if (!sheet) {
    return (
      <aside className="panel p-4 text-sm text-dungeon-ink/50 italic">{t("No character yet.")}</aside>
    );
  }

  return (
    <aside className="panel flex flex-col min-h-[240px]">
      <div className="flex border-b border-dungeon-edge">
        {TABS.map((t0) => (
          <button
            key={t0}
            onClick={() => setTab(t0)}
            className={`px-3 py-2 text-xs font-display tracking-wide transition ${
              tab === t0 ? "text-dungeon-gold border-b-2 border-dungeon-gold" : "text-dungeon-ink/60 hover:text-dungeon-ink"
            }`}
          >
            {tr(tab)}
          </button>
        ))}
      </div>
      <div className="p-4 overflow-y-auto text-sm space-y-3">
        {tab === "Overview" && <Overview sheet={sheet} />}
        {tab === "Map" && <MapPanel />}
        {tab === "Abilities" && <Abilities sheet={sheet} />}
        {tab === "Inventory" && <Inventory />}
        {tab === "Spells" && <Spells sheet={sheet} />}
        {tab === "Notes" && <Features sheet={sheet} />}
      </div>
    </aside>
  );
}

function HpBar({ hp, max }) {
  const pct = max > 0 ? Math.max(0, Math.round((hp / max) * 100)) : 0;
  return (
    <div className="h-2 bg-dungeon-bg rounded overflow-hidden border border-dungeon-edge">
      <div className="h-full bg-gradient-to-r from-dungeon-blood to-red-500" style={{ width: `${pct}%` }} />
    </div>
  );
}

function Overview({ sheet }) {
  const { t } = useLang();
  const { actions } = useStore();
  return (
    <>
      <div className="flex items-baseline justify-between">
        <span className="font-display text-lg text-dungeon-gold">{sheet.name}</span>
        <span className="text-xs uppercase tracking-wide text-dungeon-ink/60">
          {t("Lv")} {sheet.level} {[sheet.race, sheet.character_class].filter(Boolean).join(" ") || t("adventurer")}
        </span>
      </div>
      <HpBar hp={sheet.hp} max={sheet.max_hp} />
      <div className="grid grid-cols-4 gap-2 text-center">
        <Stat label={t("HP")} value={`${sheet.hp}/${sheet.max_hp}`} />
        <Stat label={t("AC")} value={sheet.ac} />
        <Stat label={t("XP")} value={sheet.xp} />
        <Stat label="Prof." value={`+${sheet.proficiency_bonus ?? 2}`} />
      </div>

      {(sheet.conditions?.length || 0) > 0 && (
        <div className="flex flex-wrap gap-1">
          {sheet.conditions.map((c) => (
            <span key={c} className="px-2 py-0.5 rounded-full bg-dungeon-blood/30 border border-dungeon-blood/60 text-xs">
              {c}
            </span>
          ))}
        </div>
      )}

      {sheet.dying && (
        <div className="rounded border border-dungeon-blood p-2 text-center space-y-2">
          <p className="text-dungeon-blood font-semibold">{t("You are dying")} — {sheet.death_save_successes}✦ / {sheet.death_save_failures}✖</p>
          <button className="btn-gold !py-1 text-xs" onClick={actions.deathSave}>
            {t("Roll a death save")}
          </button>
        </div>
      )}
      {sheet.dead && (
        <p className="text-center text-dungeon-blood font-display tracking-widest">{t("SLAIN")}</p>
      )}

      {sheet.background && <p className="text-xs italic text-dungeon-ink/70">{t("Origin:")} {sheet.background}</p>}
    </>
  );
}

function Stat({ label, value }) {
  return (
    <div className="bg-dungeon-bg rounded border border-dungeon-edge py-1.5">
      <div className="text-base font-semibold">{value}</div>
      <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/50">{label}</div>
    </div>
  );
}

function Abilities({ sheet }) {
  const { t } = useLang();
  const mods = sheet.stat_modifiers || {};
  const profSkills = new Set(sheet.skill_proficiencies || []);
  return (
    <>
      <div className="grid grid-cols-6 gap-1.5 text-center">
        {Object.entries(sheet.stats || {}).map(([key, score]) => (
          <div key={key} className="bg-dungeon-bg rounded border border-dungeon-edge py-1.5">
            <div className="text-[10px] tracking-widest text-dungeon-ink/50">{ABILITY_LABELS[key]}</div>
            <div className="font-semibold">{score}</div>
            <div className="text-xs text-dungeon-gold">
              {(mods[key] ?? 0) >= 0 ? "+" : ""}
              {mods[key] ?? 0}
            </div>
          </div>
        ))}
      </div>
      {profSkills.size > 0 && (
        <div>
          <div className="text-xs uppercase tracking-widest text-dungeon-ink/50 mb-1">{t("Proficiencies")}</div>
          <div className="flex flex-wrap gap-1">
            {[...profSkills].map((s) => (
              <span key={s} className="px-2 py-0.5 rounded-full border border-dungeon-gold/40 text-xs capitalize">
                {s.replace(/_/g, " ")}
              </span>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function Inventory() {
  const { t } = useLang();
  const { state, actions } = useStore();
  const sheet = state.character;
  const [itemName, setItemName] = useState("");
  const items = sheet.inventory || [];

  function edit(field, value) {
    if (!value.trim()) return;
    actions.editCharacter(field, value.trim());
    if (field !== "notes") setItemName("");
  }

  return (
    <>
      <ul className="space-y-1">
        {items.length === 0 && <li className="italic text-dungeon-ink/50">{t("Empty pockets.")}</li>}
        {items.map((it) => (
          <li key={it.name + (it.magic_bonus ?? 0)} className="flex items-center justify-between gap-2">
            <span>
              {it.name}
              {it.quantity > 1 && <span className="text-dungeon-ink/50"> ×{it.quantity}</span>}
              {!!it.magic_bonus && <span className="text-dungeon-gold"> +{it.magic_bonus}</span>}
              {sheet.equipped_weapon === it.name && <Tag>{t("weapon")}</Tag>}
              {sheet.equipped_armor === it.name && <Tag>{t("armor")}</Tag>}
              {sheet.equipped_shield === it.name && <Tag>{t("shield")}</Tag>}
            </span>
            <span className="flex gap-1">
              <MiniBtn onClick={() => edit("equip", it.name)}>{t("equip")}</MiniBtn>
              <MiniBtn onClick={() => edit("unequip", it.name)}>{t("unequip")}</MiniBtn>
              <MiniBtn danger onClick={() => edit("remove_item", it.name)}>{t("drop")}</MiniBtn>
            </span>
          </li>
        ))}
      </ul>
      <form
        className="flex gap-2 pt-1"
        onSubmit={(e) => {
          e.preventDefault();
          edit("add_item", itemName);
        }}
      >
        <input
          className="flex-1 bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1 text-sm"
          placeholder={t("Add an item…")}
          value={itemName}
          onChange={(e) => setItemName(e.target.value)}
        />
        <button type="submit" className="btn-gold !py-1 !px-3 text-xs">
          {t("Add")}
        </button>
      </form>
    </>
  );
}

function Spells({ sheet }) {
  const { t } = useLang();
  const slots = sheet.spell_slots || {};
  const maxSlots = sheet.max_spell_slots || {};
  const levels = Object.keys(maxSlots).sort();
  const known = sheet.known_spells || [];
  const saveDc = sheet.spell_save_dc;

  if (known.length === 0 && levels.length === 0) {
    return <p className="italic text-dungeon-ink/50">{t("No spellcasting.")}</p>;
  }
  return (
    <>
      {levels.length > 0 && (
        <div className="space-y-1">
          {levels.map((lvl) => (
            <div key={lvl} className="flex items-center gap-2 text-sm">
              <span className="w-14 text-dungeon-ink/60">{t("Lv")} {lvl}</span>
              {Array.from({ length: maxSlots[lvl] }).map((_, i) => (
                <span key={i} className={`w-3 h-3 rounded-full border ${i < slots[lvl] ? "bg-sky-400 border-sky-400" : "border-dungeon-edge"}`} />
              ))}
              <span className="text-xs text-dungeon-ink/50 ml-1">
                {slots[lvl]}/{maxSlots[lvl]}
              </span>
            </div>
          ))}
          {saveDc != null && <p className="text-xs text-dungeon-ink/60">{t("Spell save DC")} {saveDc}</p>}
        </div>
      )}
      {known.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {known.map((s) => (
            <span key={s} className="px-2 py-0.5 rounded-full border border-sky-400/40 text-xs capitalize">
              {s.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

function Features({ sheet }) {
  const { t } = useLang();
  const { actions } = useStore();
  const [notes, setNotes] = useState(sheet.notes || "");
  const dirty = notes !== (sheet.notes || "");
  return (
    <div className="space-y-3">
      {(sheet.class_features?.length ?? 0) > 0 && (
        <details open>
          <summary className="cursor-pointer text-xs uppercase tracking-widest text-dungeon-ink/50">{t("Class features")}</summary>
          <ul className="list-disc pl-5 mt-1 space-y-1 text-xs">
            {sheet.class_features.map((f) => (
              <li key={f.name || f}>{f.text ? `${f.name}: ${f.text}` : f}</li>
            ))}
          </ul>
        </details>
      )}
      {(sheet.racial_traits?.length ?? 0) > 0 && (
        <details>
          <summary className="cursor-pointer text-xs uppercase tracking-widest text-dungeon-ink/50">{t("Racial traits")}</summary>
          <ul className="list-disc pl-5 mt-1 space-y-1 text-xs">
            {sheet.racial_traits.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </details>
      )}
      <div>
        <div className="text-xs uppercase tracking-widest text-dungeon-ink/50 mb-1">Notes</div>
        <textarea
          className="w-full h-24 bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1 text-sm focus:border-dungeon-gold outline-none"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder={t("Your private journal…")}
        />
        <button
          className={`btn-gold !py-1 !px-3 text-xs mt-1 ${dirty ? "" : "opacity-40 pointer-events-none"}`}
          onClick={() => actions.editCharacter("notes", notes)}
        >
          {t("Save notes")}
        </button>
      </div>
    </div>
  );
}

function Tag({ children }) {
  return <span className="ml-1 text-[10px] uppercase tracking-wide text-dungeon-gold/80">[{children}]</span>;
}

function MiniBtn({ children, onClick, danger }) {
  return (
    <button
      onClick={onClick}
      className={`text-[10px] px-1.5 py-0.5 rounded border transition ${
        danger ? "border-dungeon-blood/50 text-dungeon-blood/90 hover:bg-dungeon-blood/20" : "border-dungeon-edge text-dungeon-ink/60 hover:text-dungeon-ink"
      }`}
    >
      {children}
    </button>
  );
}
