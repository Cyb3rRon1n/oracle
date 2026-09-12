// Tabbed character sheet - the owner's full view (state.character is the
// owner-only payload from state_sync/character_update: inventory, stats,
// spells, class features, proficiencies). Bookkeeping edits ride
// character_edit; everything else here is read-only projection.

import { useState } from "react";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";
import InventoryPanel from "./InventoryPanel.jsx";
import Avatar from "./Avatar.jsx";

const ABILITY_LABELS = {
  str: "STR",
  dex: "DEX",
  con: "CON",
  int: "INT",
  wis: "WIS",
  cha: "CHA",
};

const TABS = ["Overview", "Abilities", "Inventory", "Spells", "Notes"];

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
      <div className="flex flex-wrap border-b border-dungeon-edge">
        {TABS.map((t0) => (
          <button
            key={t0}
            onClick={() => setTab(t0)}
            className={`px-3 py-2 text-xs font-display tracking-wide transition ${
              tab === t0 ? "text-dungeon-gold border-b-2 border-dungeon-gold" : "text-dungeon-ink/60 hover:text-dungeon-ink"
            }`}
          >
            {tr(t0)}
          </button>
        ))}
      </div>
      <div className="p-4 overflow-y-auto text-sm space-y-3">
        {tab === "Overview" && <Overview sheet={sheet} />}
        {tab === "Abilities" && <Abilities sheet={sheet} />}
        {tab === "Inventory" && <Inventory />}
        {tab === "Spells" && <Spells sheet={sheet} />}
        {tab === "Notes" && <Features sheet={sheet} />}
      </div>
    </aside>
  );
}

function HpBar({ hp, max }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, Math.round((hp / max) * 100))) : 0;
  // Colour by how hurt you are, not a flat blood-red at full health.
  const fill =
    pct > 50 ? "bg-emerald-600" : pct > 25 ? "bg-amber-500" : "bg-dungeon-blood";
  return (
    <div className="h-2 bg-dungeon-bg rounded overflow-hidden border border-dungeon-edge">
      <div className={`h-full ${fill} transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function Overview({ sheet }) {
  const { t } = useLang();
  const { state, actions } = useStore();
  return (
    <>
      <div className="flex items-start gap-2">
        <Avatar
          sheet={sheet}
          pending={state.portraitPending}
          progress={state.portraitProgress}
          onGenerate={actions.generatePortrait}
          t={t}
        />
        <div className="flex-1 flex items-baseline justify-between">
          <span className="font-display text-lg text-dungeon-gold">{sheet.name}</span>
          <span className="text-xs uppercase tracking-wide text-dungeon-ink/60">
            {t("Lv")} {sheet.level} {[sheet.race, sheet.character_class].filter(Boolean).join(" ") || t("adventurer")}
          </span>
        </div>
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
  return <InventoryPanel sheet={sheet} edit={actions.editCharacter} t={t} />;
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
