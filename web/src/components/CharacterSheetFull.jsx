// Full-page character sheet overlay - the classic WotC 5e sheet's
// information architecture (abilities + skills left, combat + HP + gear
// center, features + spells right) in Oracle's dark-dungeon theme.
//
// Sections Oracle tracks are populated from the owner-only sheet payload
// (state.character). Sections the paper sheet has that Oracle doesn't yet
// track render greyed with a <Planned> tag - see docs/character-sheet-gaps.md,
// which is the same list.

import { useEffect, useState } from "react";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";
import { ABILITIES, ABILITY_LABEL, SKILLS, skillLabel, fmtMod, passivePerception, passiveScore } from "../lib/dnd5e.js";

export default function CharacterSheetFull({ onClose }) {
  const { state, actions } = useStore();
  const { t } = useLang();
  const sheet = state.character;

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!sheet) return null;

  const mod = (a) => sheet.stat_modifiers?.[a] ?? 0;
  const prof = sheet.proficiency_bonus ?? 2;
  const saveProfs = sheet.saving_throw_proficiencies || [];
  const skillProfs = sheet.skill_proficiencies || [];

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-dungeon-bg">
      <div className="max-w-6xl mx-auto p-4 sm:p-6">
        {/* header strip */}
        <div className="panel p-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="font-display text-2xl text-dungeon-gold">{sheet.name}</div>
            <div className="text-sm text-dungeon-ink/70">
              {t("Lv")} {sheet.level} · {[sheet.race, sheet.character_class].filter(Boolean).join(" ") || t("adventurer")}
            </div>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <MiniStat
              label={t("XP")}
              value={sheet.xp_next_level ? `${sheet.xp} / ${sheet.xp_next_level}` : sheet.xp}
            />
            {sheet.alignment && <MiniStat label={t("Alignment")} value={sheet.alignment} />}
            <button
              className="btn-gold !py-1 !px-3 text-sm"
              onClick={onClose}
              aria-label={t("Close")}
            >
              ✕ {t("Close")}
            </button>
          </div>
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-3">
          {/* ---- LEFT: abilities, saves, skills ---- */}
          <div className="space-y-4">
            <div className="panel p-3 grid grid-cols-3 gap-2">
              {ABILITIES.map((a) => (
                <div key={a} className="bg-dungeon-bg rounded border border-dungeon-edge p-2 text-center">
                  <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/50">{a}</div>
                  <div className="text-xl font-semibold">{sheet.stats?.[a] ?? "—"}</div>
                  <div className="text-dungeon-gold text-sm">{fmtMod(mod(a))}</div>
                </div>
              ))}
            </div>

            <div className="panel p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-dungeon-ink/60">{t("Proficiency bonus")}</span>
                <span className="text-dungeon-gold font-semibold">{fmtMod(prof)}</span>
              </div>
            </div>

            <Section title={t("Saving throws")}>
              {ABILITIES.map((a) => {
                const p = saveProfs.includes(a);
                return (
                  <Row key={a} proficient={p} label={ABILITY_LABEL[a]} value={fmtMod(mod(a) + (p ? prof : 0))} />
                );
              })}
            </Section>

            <Section title={t("Skills")}>
              {SKILLS.map(([key, ability]) => {
                const p = skillProfs.includes(key);
                return (
                  <Row
                    key={key}
                    proficient={p}
                    label={skillLabel(key)}
                    tag={ability.toUpperCase()}
                    value={fmtMod(mod(ability) + (p ? prof : 0))}
                  />
                );
              })}
            </Section>

            <div className="panel p-3 text-sm space-y-1">
              {[
                [t("Passive perception"), passivePerception(sheet)],
                [t("Passive investigation"), passiveScore(sheet, "investigation")],
                [t("Passive insight"), passiveScore(sheet, "insight")],
              ].map(([label, val]) => (
                <div key={label} className="flex justify-between">
                  <span className="text-dungeon-ink/60">{label}</span>
                  <span className="font-semibold">{val}</span>
                </div>
              ))}
            </div>
          </div>

          {/* ---- CENTER: combat, hp, gear ---- */}
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-2">
              <MiniStat box label={t("Armor class")} value={sheet.ac} />
              <MiniStat box label={t("Initiative")} value={fmtMod(mod("dex"))} />
              <MiniStat box label={t("Speed")} value={`${sheet.speed ?? 30} ${t("ft")}`} />
            </div>

            <Section title={t("Hit points")}>
              <div className="text-center py-1">
                <span className="text-2xl font-semibold">{sheet.hp}</span>
                <span className="text-dungeon-ink/50"> / {sheet.max_hp}</span>
                {sheet.temp_hp > 0 && <span className="text-sky-400 text-sm"> +{sheet.temp_hp}</span>}
              </div>
              <HpBar hp={sheet.hp} max={sheet.max_hp} />
              <div className="flex justify-between mt-2 text-xs">
                <span className="text-dungeon-ink/60">
                  {t("Temp HP")} <b className="text-dungeon-ink">{sheet.temp_hp || 0}</b>
                </span>
                <Planned label={t("Hit dice")} />
              </div>
            </Section>

            <Section title={t("Death saves")}>
              {sheet.dead ? (
                <p className="text-center text-dungeon-blood font-display tracking-widest">{t("SLAIN")}</p>
              ) : (
                <div className={`space-y-1 ${sheet.dying ? "" : "opacity-50"}`}>
                  <Pips label={t("Successes")} filled={sheet.death_save_successes || 0} tone="bg-emerald-500" />
                  <Pips label={t("Failures")} filled={sheet.death_save_failures || 0} tone="bg-dungeon-blood" />
                  {sheet.dying && (
                    <button className="btn-gold !py-1 text-xs mt-1 w-full" onClick={actions.deathSave}>
                      {t("Roll a death save")}
                    </button>
                  )}
                </div>
              )}
            </Section>

            <Section title={t("Attacks")}>
              {(sheet.attacks?.length ?? 0) === 0 ? (
                <p className="text-dungeon-ink/50 italic text-sm">{t("No attacks.")}</p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-[10px] uppercase tracking-widest text-dungeon-ink/50">
                      <th className="text-left font-normal">{t("Name")}</th>
                      <th className="text-right font-normal">{t("Atk")}</th>
                      <th className="text-right font-normal">{t("Damage")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sheet.attacks.map((a) => (
                      <tr key={a.kind + a.name}>
                        <td className="py-0.5">
                          {a.name}
                          {a.kind === "spell" && <span className="text-sky-400/70 text-[10px] ml-1">✦</span>}
                        </td>
                        <td className="text-right tabular-nums">{a.to_hit}</td>
                        <td className="text-right tabular-nums text-dungeon-ink/80">{a.damage}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Section>

            <Section title={t("Equipment")}>
              <Equipment sheet={sheet} edit={actions.editCharacter} t={t} />
            </Section>

            <div className="flex gap-2">
              <Planned box label={t("Currency")} />
              <Planned box label={t("Inspiration")} />
            </div>
          </div>

          {/* ---- RIGHT: features, origin, spells, notes ---- */}
          <div className="space-y-4">
            <Section title={t("Class features")}>
              <List items={sheet.class_features} empty={t("None yet.")} />
            </Section>

            {(sheet.racial_traits?.length ?? 0) > 0 && (
              <Section title={t("Racial traits")}>
                <List items={sheet.racial_traits} />
              </Section>
            )}

            {sheet.background && (
              <Section title={t("Origin")}>
                <p className="text-sm italic text-dungeon-ink/70">{sheet.background}</p>
              </Section>
            )}

            <Section title={t("Personality")}>
              <div className="space-y-2">
                {[
                  ["personality", t("Personality")],
                  ["ideals", t("Ideals")],
                  ["bonds", t("Bonds")],
                  ["flaws", t("Flaws")],
                  ["alignment", t("Alignment")],
                ].map(([field, label]) => (
                  <RpField
                    key={field}
                    label={label}
                    value={sheet[field] || ""}
                    save={(v) => actions.editCharacter(field, v)}
                  />
                ))}
              </div>
            </Section>

            <Section title={t("Proficiencies & languages")}>
              {skillProfs.length > 0 && (
                <div className="flex flex-wrap gap-1 mb-2">
                  {skillProfs.map((s) => (
                    <span key={s} className="px-2 py-0.5 rounded-full border border-dungeon-gold/40 text-xs capitalize">
                      {s.replace(/_/g, " ")}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex gap-3 text-xs">
                <Planned label={t("Armor / weapons / tools")} />
                <Planned label={t("Languages")} />
              </div>
            </Section>

            <Section title={t("Spellcasting")}>
              <Spellcasting sheet={sheet} t={t} />
            </Section>

            <Section title={t("Notes")}>
              <Notes value={sheet.notes || ""} save={(v) => actions.editCharacter("notes", v)} t={t} />
            </Section>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------- small pieces ---------- */

function Planned({ label, box }) {
  const { t } = useLang();
  const tag = (
    <span className="text-[10px] uppercase tracking-wide text-dungeon-ink/40 border border-dungeon-edge rounded px-1">
      {t("planned")}
    </span>
  );
  if (box) {
    return (
      <div className="flex-1 bg-dungeon-bg rounded border border-dashed border-dungeon-edge p-2 text-center opacity-60">
        <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/50">{label}</div>
        <div className="mt-1">{tag}</div>
      </div>
    );
  }
  return label ? (
    <span className="text-xs text-dungeon-ink/40">
      {label} {tag}
    </span>
  ) : (
    tag
  );
}

function Section({ title, children }) {
  return (
    <div className="panel p-3">
      <div className="text-xs uppercase tracking-widest text-dungeon-gold/70 mb-2">{title}</div>
      {children}
    </div>
  );
}

function Row({ proficient, label, value, tag }) {
  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <span className={`w-2.5 h-2.5 rounded-full border ${proficient ? "bg-dungeon-gold border-dungeon-gold" : "border-dungeon-edge"}`} />
      <span className="flex-1">
        {label}
        {tag && <span className="text-dungeon-ink/40 text-[10px] ml-1">{tag}</span>}
      </span>
      <span className="tabular-nums text-dungeon-ink/90">{value}</span>
    </div>
  );
}

function MiniStat({ label, value, box }) {
  if (box) {
    return (
      <div className="flex-1 bg-dungeon-bg rounded border border-dungeon-edge p-2 text-center">
        <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/50">{label}</div>
        <div className="text-lg font-semibold">{value}</div>
      </div>
    );
  }
  return (
    <span className="text-dungeon-ink/70">
      {label} <b className="text-dungeon-ink">{value}</b>
    </span>
  );
}

function HpBar({ hp, max }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, Math.round((hp / max) * 100))) : 0;
  const fill = pct > 50 ? "bg-emerald-600" : pct > 25 ? "bg-amber-500" : "bg-dungeon-blood";
  return (
    <div className="h-2 bg-dungeon-bg rounded overflow-hidden border border-dungeon-edge">
      <div className={`h-full ${fill}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function Pips({ label, filled, tone }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-16 text-dungeon-ink/60">{label}</span>
      {[0, 1, 2].map((i) => (
        <span key={i} className={`w-3 h-3 rounded-full border ${i < filled ? tone : "border-dungeon-edge"}`} />
      ))}
    </div>
  );
}

function List({ items, empty }) {
  if (!items?.length) return empty ? <p className="text-dungeon-ink/50 italic text-sm">{empty}</p> : null;
  return (
    <ul className="list-disc pl-5 space-y-1 text-sm">
      {items.map((f, i) => (
        <li key={f.name || f || i}>{f?.text ? `${f.name}: ${f.text}` : f}</li>
      ))}
    </ul>
  );
}

function Equipment({ sheet, edit, t }) {
  const [name, setName] = useState("");
  const items = sheet.inventory || [];
  const add = (e) => {
    e.preventDefault();
    if (name.trim()) {
      edit("add_item", name.trim());
      setName("");
    }
  };
  return (
    <>
      <ul className="space-y-1 text-sm">
        {items.length === 0 && <li className="italic text-dungeon-ink/50">{t("Empty pockets.")}</li>}
        {items.map((it) => (
          <li key={it.name + (it.magic_bonus ?? 0)} className="flex items-center justify-between gap-2">
            <span>
              {it.name}
              {it.quantity > 1 && <span className="text-dungeon-ink/50"> ×{it.quantity}</span>}
              {!!it.magic_bonus && <span className="text-dungeon-gold"> +{it.magic_bonus}</span>}
              {sheet.equipped_weapon === it.name && <Slot>{t("weapon")}</Slot>}
              {sheet.equipped_armor === it.name && <Slot>{t("armor")}</Slot>}
              {sheet.equipped_shield === it.name && <Slot>{t("shield")}</Slot>}
            </span>
            <span className="flex gap-1">
              <Mini onClick={() => edit("equip", it.name)}>{t("equip")}</Mini>
              <Mini onClick={() => edit("unequip", it.name)}>{t("unequip")}</Mini>
              <Mini danger onClick={() => edit("remove_item", it.name)}>{t("drop")}</Mini>
            </span>
          </li>
        ))}
      </ul>
      <form className="flex gap-2 pt-2" onSubmit={add}>
        <input
          className="flex-1 bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1 text-sm"
          placeholder={t("Add an item…")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button type="submit" className="btn-gold !py-1 !px-3 text-xs">
          {t("Add")}
        </button>
      </form>
    </>
  );
}

function Spellcasting({ sheet, t }) {
  const known = sheet.known_spells || [];
  const maxSlots = sheet.max_spell_slots || {};
  const slots = sheet.spell_slots || {};
  const levels = Object.keys(maxSlots).sort();
  if (known.length === 0 && levels.length === 0) {
    return <p className="text-dungeon-ink/50 italic text-sm">{t("Not a spellcaster.")}</p>;
  }
  return (
    <div className="space-y-2 text-sm">
      <div className="flex gap-4 text-xs">
        {sheet.spell_save_dc != null && (
          <span className="text-dungeon-ink/70">
            {t("Save DC")} <b className="text-dungeon-ink">{sheet.spell_save_dc}</b>
          </span>
        )}
        {sheet.spell_attack_bonus != null && (
          <span className="text-dungeon-ink/70">
            {t("Attack")} <b className="text-dungeon-ink">{fmtMod(sheet.spell_attack_bonus)}</b>
          </span>
        )}
      </div>
      {levels.map((lvl) => (
        <div key={lvl} className="flex items-center gap-2">
          <span className="w-14 text-dungeon-ink/60 text-xs">{t("Lv")} {lvl}</span>
          {Array.from({ length: maxSlots[lvl] }).map((_, i) => (
            <span key={i} className={`w-3 h-3 rounded-full border ${i < (slots[lvl] ?? 0) ? "bg-sky-400 border-sky-400" : "border-dungeon-edge"}`} />
          ))}
          <span className="text-xs text-dungeon-ink/50">{slots[lvl] ?? 0}/{maxSlots[lvl]}</span>
        </div>
      ))}
      {known.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1">
          {known.map((s) => (
            <span key={s} className="px-2 py-0.5 rounded-full border border-sky-400/40 text-xs capitalize">
              {s.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// One RP line (personality / ideals / bonds / flaws). Seeded at character
// creation, player-editable. Saves on blur when changed; the server
// ignores an empty value, same as notes, so this doesn't try to clear.
function RpField({ label, value, save }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    const v = text.trim();
    if (v && v !== value) save(v);
  };
  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-wide text-dungeon-ink/50">{label}</span>
      <input
        className="mt-0.5 w-full bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1 text-sm focus:border-dungeon-gold outline-none"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
    </label>
  );
}

function Notes({ value, save, t }) {
  const [text, setText] = useState(value);
  const dirty = text !== value;
  return (
    <div>
      <textarea
        className="w-full h-28 bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1 text-sm focus:border-dungeon-gold outline-none"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("Your private journal…")}
      />
      <button
        className={`btn-gold !py-1 !px-3 text-xs mt-1 ${dirty ? "" : "opacity-40 pointer-events-none"}`}
        onClick={() => save(text)}
      >
        {t("Save notes")}
      </button>
    </div>
  );
}

function Slot({ children }) {
  return <span className="ml-1 text-[10px] uppercase tracking-wide text-dungeon-gold/80">[{children}]</span>;
}

function Mini({ children, onClick, danger }) {
  return (
    <button
      onClick={onClick}
      className={`text-[10px] px-1.5 py-0.5 rounded border transition ${
        danger
          ? "border-dungeon-blood/50 text-dungeon-blood/90 hover:bg-dungeon-blood/20"
          : "border-dungeon-edge text-dungeon-ink/60 hover:text-dungeon-ink"
      }`}
    >
      {children}
    </button>
  );
}
