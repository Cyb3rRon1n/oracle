// The turn's structured resolution (scene_update) plus the campaign's
// server-held tension meters (clocks from world_update). Everything here is
// decided server-side; the client only renders it.

import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

export default function ScenePanel({ onSuggest }) {
  const { state } = useStore();
  const { t } = useLang();
  const scene = state.scene;
  const clocks = state.world.clocks || [];
  if (!scene && clocks.length === 0 && !(state.world.objectives || []).length) return null;

  return (
    <aside className="panel p-4 space-y-3 text-sm">
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

      {scene?.points_of_interest?.length > 0 && (
        <Section title={t("Of interest")}>
          <div className="flex flex-wrap gap-1">
            {scene.points_of_interest.map((p) => (
              <button
                key={p}
                onClick={() => onSuggest(`${t('Examine')} ${p}`)}
                className="px-2 py-0.5 rounded-full border border-sky-400/40 text-xs hover:bg-sky-400/10"
              >
                {p}
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
                key={a}
                onClick={() => onSuggest(a)}
                className="text-left px-2 py-1 rounded border border-dungeon-edge hover:border-dungeon-gold hover:text-dungeon-gold text-xs transition"
              >
                {a}
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
