// The campaign map (docs/protocol.md "Protocol v2 additions - Map"): a
// graph of locations with optional coordinate hints. The server owns the
// graph; this panel only lays it out. Nodes the DM never placed fan out on
// an auto-layout ring; placed nodes render at their coordinates with
// bounds-fit scaling. The world's current location gets the gold ring.

import { useMemo } from "react";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

const W = 320;
const H = 260;
const PAD = 34;

function layout(nodes, edges) {
  const placed = nodes.filter((n) => n.x != null || n.y != null);
  const floating = nodes.filter((n) => n.x == null && n.y == null);

  const positions = new Map();

  if (placed.length > 0) {
    const xs = placed.map((n) => n.x ?? 0);
    const ys = placed.map((n) => n.y ?? 0);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const spanX = maxX - minX || 1;
    const spanY = maxY - minY || 1;
    for (const n of placed) {
      positions.set(n.name, {
        x: PAD + ((n.x ?? 0) - minX) / spanX * (W - 2 * PAD),
        y: PAD + ((n.y ?? 0) - minY) / spanY * (H - 2 * PAD),
      });
    }
  }

  // Floating nodes: ring around the free center of mass.
  const cx = W / 2;
  const cy = placed.length ? H * 0.28 : H / 2;
  const r = placed.length ? Math.min(W, H) * 0.24 : Math.min(W, H) * 0.32;
  floating.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(floating.length, 1) - Math.PI / 2;
    positions.set(n.name, { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) });
  });

  return { positions };
}

export default function MapPanel() {
  const { state } = useStore();
  const { t } = useLang();
  const map = state.world.map;
  const { positions } = useMemo(() => layout(map?.nodes ?? [], map?.edges ?? []), [map]);

  if (!map?.nodes?.length) {
    return <p className="italic text-dungeon-ink/50 text-sm">{t("The map has not been charted yet.")}</p>;
  }
  const here = (state.world.location || "").toLowerCase();

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full bg-dungeon-bg rounded border border-dungeon-edge">
        {map.edges.map(([a, b]) => {
          const pa = positions.get(a);
          const pb = positions.get(b);
          if (!pa || !pb) return null;
          return (
            <line
              key={a + "|" + b}
              x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y}
              stroke="#3f3a33"
              strokeWidth={2}
              strokeDasharray="4 3"
            />
          );
        })}
        {map.nodes.map((n) => {
          const p = positions.get(n.name);
          if (!p) return null;
          const isHere = n.name.toLowerCase() === here;
          return (
            <g key={n.name} transform={`translate(${p.x}, ${p.y})`}>
              {isHere && <circle r={16} fill="none" stroke="#c9a959" strokeWidth={2}>
                <animate attributeName="r" values="13;17;13" dur="2s" repeatCount="indefinite" />
              </circle>}
              <text textAnchor="middle" dy="-8" fontSize="14">{n.icon || "•"}</text>
              <text
                textAnchor="middle"
                dy="10"
                fontSize="7.5"
                fill={isHere ? "#c9a959" : "#d6cdbb"}
                className="font-display"
              >
                {n.name.length > 18 ? n.name.slice(0, 17) + "…" : n.name}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="text-xs text-dungeon-ink/50 mt-1">
        {t("You are at")} <span className="text-dungeon-gold">{state.world.location || t("an unknown place")}</span>.
      </p>
    </div>
  );
}
