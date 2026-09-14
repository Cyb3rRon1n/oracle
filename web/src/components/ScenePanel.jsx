// The turn's structured resolution (scene_update) plus the campaign's
// server-held tension meters (clocks from world_update). Everything here is
// decided server-side; the client only renders it.

import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";
import { skillLabel } from "../lib/dnd5e.js";
import Avatar from "./Avatar.jsx";

const RANGE_LABELS = { melee: "Melee", near: "Near", far: "Far" };

export default function ScenePanel({ onSuggest }) {
  const { state, actions } = useStore();
  const { t } = useLang();
  const scene = state.scene;
  const clocks = state.world.clocks || [];
  // A defeated NPC drops out - gone from active play. hp <= 0, not `dead`:
  // an NPC has no death-save/dead concept at all (that's player-only, see
  // server/state.py's grant_death_save), the same check server/views.py's
  // own DM-facing NPC roster (_npc_roster) already uses.
  const npcs = Object.entries(state.npcs || {}).filter(([, npc]) => npc.hp > 0);
  const showCombatants = state.inCombat && npcs.length > 0;
  const players = Object.values(state.players || {});
  const showTurnOrder = state.inCombat && state.turnOrder.length > 0;
  if (!scene && clocks.length === 0 && !(state.world.objectives || []).length && !showCombatants && players.length === 0)
    return null;

  return (
    <aside className="panel p-4 space-y-3 text-sm max-h-[60vh] overflow-y-auto">
      {showTurnOrder && (
        <Section title={t("Turn order")}>
          <div className="flex flex-wrap items-center gap-1.5">
            {state.turnOrder.map((pid, i) => {
              const p = state.players[pid];
              const active = pid === state.currentTurn;
              return (
                <span key={pid} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-dungeon-ink/30">→</span>}
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs border ${
                      active
                        ? "border-dungeon-gold text-dungeon-gold bg-dungeon-gold/10"
                        : "border-dungeon-edge text-dungeon-ink/60"
                    }`}
                  >
                    {p?.name || pid}
                    {pid === state.me && ` (${t("you")})`}
                  </span>
                </span>
              );
            })}
          </div>
        </Section>
      )}

      {players.length > 0 && (
        <Section title={t("Party")}>
          <div className="space-y-1.5">
            {players.map((p) => (
              <div key={p.player_id} className="flex items-center gap-2">
                <Avatar sheet={p} compact />
                <span className="flex-1 min-w-0 truncate">
                  {p.name}
                  {p.player_id === state.me && <span className="text-dungeon-ink/40"> ({t("you")})</span>}
                </span>
                {p.dead ? (
                  <span className="text-[10px] text-dungeon-blood uppercase tracking-wide">{t("Slain")}</span>
                ) : p.dying ? (
                  <span className="text-[10px] text-dungeon-blood uppercase tracking-wide">{t("Dying")}</span>
                ) : (
                  <>
                    {p.conditions?.length > 0 && (
                      <span className="text-[9px] text-dungeon-ink/50">{p.conditions.join(", ")}</span>
                    )}
                    <HpBar hp={p.hp} max={p.max_hp} className="w-14" />
                    <span className="text-[10px] text-dungeon-ink/50 w-14 text-right shrink-0">
                      {p.hp}/{p.max_hp} {t("HP")}
                    </span>
                  </>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {(state.world.objectives || []).some((o) => o.status === "active") && (
        <Section title={t("Objectives")}>
          <ul className="list-disc pl-5 space-y-0.5">
            {state.world.objectives
              .filter((o) => o.status === "active")
              .map((o) => (
                <li key={o.text}>{o.text}</li>
              ))}
          </ul>
        </Section>
      )}

      {scene?.npcs_present?.length > 0 && (
        <Section title={t("Present")}>
          <div className="flex flex-wrap gap-1">
            {scene.npcs_present.map((n) => (
              <span key={n} className="px-2 py-0.5 rounded-full border border-dungeon-gold/40 text-xs">
                {n}
              </span>
            ))}
          </div>
        </Section>
      )}

      {showCombatants && (
        <Section title={t("Combatants")}>
          <div className="grid grid-cols-2 gap-2">
            {npcs.map(([name, npc]) => (
              <div key={name} className="rounded border border-dungeon-edge p-2 space-y-1.5">
                <div className="flex items-center gap-1.5">
                  <Avatar sheet={{ name }} compact />
                  <span className="flex-1 min-w-0 truncate">{name}</span>
                  <span className="shrink-0 text-[9px] uppercase tracking-wide text-dungeon-gold/70 border border-dungeon-gold/40 rounded px-1 py-0.5">
                    {RANGE_LABELS[npc.range_band] || npc.range_band}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <HpBar hp={npc.hp} max={npc.max_hp} className="flex-1" />
                  <span className="text-[10px] text-dungeon-ink/50 shrink-0">
                    {npc.hp}/{npc.max_hp}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2 min-h-[18px]">
                  {npc.disposition && npc.disposition !== "neutral" ? (
                    <span className="text-[10px] text-dungeon-ink/50">{npc.disposition}</span>
                  ) : (
                    <span />
                  )}
                  {npc.range_band === "melee" && (
                    <button
                      onClick={() => actions.editCharacter("shove", name)}
                      className="text-[10px] px-1.5 py-0.5 rounded border border-dungeon-edge text-dungeon-ink/60 hover:text-dungeon-ink hover:border-dungeon-gold transition"
                    >
                      {t("shove")}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {scene?.points_of_interest?.length > 0 && (
        <Section title={t("Of interest")}>
          <div className="flex flex-col gap-1">
            {scene.points_of_interest.map((p) => (
              <button
                key={p.text}
                onClick={() => onSuggest(p.text)}
                className="text-left px-2 py-1 rounded border border-sky-400/40 hover:bg-sky-400/10 text-xs transition flex items-center justify-between gap-2"
              >
                <span>{p.text}</span>
                {p.skill && (
                  <span className="shrink-0 text-[9px] uppercase tracking-wide text-dungeon-gold/70 border border-dungeon-gold/40 rounded px-1 py-0.5">
                    {skillLabel(p.skill)}
                    {p.dc != null && ` DC ${p.dc}`}
                  </span>
                )}
              </button>
            ))}
          </div>
        </Section>
      )}

      {scene?.suggested_actions?.length > 0 && (
        <Section title={t("You might…")}>
          <div className="flex flex-col gap-1">
            {scene.suggested_actions.map((a) => (
              <button
                key={a.text}
                onClick={() => onSuggest(a.text)}
                className="text-left px-2 py-1 rounded border border-dungeon-edge hover:border-dungeon-gold hover:text-dungeon-gold text-xs transition flex items-center justify-between gap-2"
              >
                <span>{a.text}</span>
                {a.skill && (
                  <span className="shrink-0 text-[9px] uppercase tracking-wide text-dungeon-gold/70 border border-dungeon-gold/40 rounded px-1 py-0.5">
                    {skillLabel(a.skill)}
                    {a.dc != null && ` DC ${a.dc}`}
                  </span>
                )}
              </button>
            ))}
          </div>
        </Section>
      )}

      {clocks.length > 0 && (
        <Section title={t("Clocks")}>
          <div className="space-y-1.5">
            {clocks.map((c) => (
              <div key={c.name} className="flex items-center gap-2">
                <span className="text-xs flex-1">{c.name}</span>
                <ClockPips filled={c.filled} segments={c.segments} />
              </div>
            ))}
          </div>
        </Section>
      )}
    </aside>
  );
}

function HpBar({ hp, max, className = "w-10" }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, Math.round((hp / max) * 100))) : 0;
  const fill = pct > 50 ? "bg-emerald-600" : pct > 25 ? "bg-amber-500" : "bg-dungeon-blood";
  return (
    <span className={`${className} h-1.5 bg-dungeon-bg rounded overflow-hidden border border-dungeon-edge shrink-0`}>
      <span className={`block h-full ${fill}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

function ClockPips({ filled, segments }) {
  return (
    <span className="flex gap-0.5">
      {Array.from({ length: segments }).map((_, i) => (
        <span key={i} className={`w-2.5 h-2.5 rounded-full border ${i < filled ? "bg-dungeon-blood border-red-400" : "border-dungeon-edge"}`} />
      ))}
    </span>
  );
}

function Section({ title, children }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/50 mb-1">{title}</div>
      {children}
    </div>
  );
}
