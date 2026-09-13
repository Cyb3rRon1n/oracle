// Portrait avatar + style picker + generate/regenerate control, shared by
// the full sheet overlay and the tabbed sheet's Overview tab. No portrait
// yet -> initials on a deterministic color swatch (hashed from the name,
// no lookup table needed) - same fallback idea as nightwire's own
// portraitFor(), simplified since oracle has no role enum to key a real
// palette off of.

import { useState } from "react";

// Prompt-text-only style tags (server/portrait.py's STYLE_PRESETS) - kept
// in sync by hand, small and fixed enough that a server-announces-styles
// mechanism would be overkill for four entries.
const STYLE_OPTIONS = [
  ["fantasy", "Fantasy painting"],
  ["anime", "Anime"],
  ["comic", "Comic book"],
  ["realistic", "Realistic photo"],
];

export default function Avatar({ sheet, pending, progress, onGenerate, t, compact }) {
  const [style, setStyle] = useState("fantasy");
  const name = sheet.name || "?";
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 0);
  const pct = progress?.total ? Math.round((progress.step / progress.total) * 100) : null;

  const circle = (
    <div
      className={`${compact ? "w-8 h-8" : "w-14 h-14"} rounded-full border border-dungeon-edge overflow-hidden flex items-center justify-center shrink-0`}
      style={sheet.portrait ? undefined : { backgroundColor: `hsl(${hue}, 35%, 25%)` }}
    >
      {sheet.portrait ? (
        <img src={sheet.portrait} alt="" className="w-full h-full object-cover" />
      ) : (
        <span className={`font-display text-dungeon-ink/80 ${compact ? "text-[10px]" : "text-sm"}`}>{initials}</span>
      )}
    </div>
  );

  if (compact) return circle;

  return (
    <div className="flex flex-col items-center gap-1 shrink-0 w-24">
      {circle}

      {pending ? (
        <div className="w-full">
          <div className="h-1 bg-dungeon-bg rounded overflow-hidden border border-dungeon-edge">
            <div
              className="h-full bg-dungeon-gold transition-all"
              style={{ width: pct != null ? `${pct}%` : "15%" }}
            />
          </div>
          <div className="text-[9px] text-dungeon-ink/50 text-center mt-0.5">
            {pct != null ? `${t("Generating…")} ${pct}%` : t("Generating…")}
          </div>
        </div>
      ) : (
        <>
          <select
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            className="text-[9px] bg-dungeon-bg border border-dungeon-edge rounded px-1 py-0.5 w-full text-dungeon-ink/70"
          >
            {STYLE_OPTIONS.map(([key, label]) => (
              <option key={key} value={key}>
                {t(label)}
              </option>
            ))}
          </select>
          <button
            onClick={() => onGenerate(style)}
            className="text-[9px] text-dungeon-ink/50 hover:text-dungeon-ink underline"
          >
            {sheet.portrait ? t("Regenerate") : t("Generate portrait")}
          </button>
        </>
      )}
    </div>
  );
}
