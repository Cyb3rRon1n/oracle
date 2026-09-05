// World-context lorebook picker. The server (server/lorebook.py) turns
// files in its world_context/ dir into keyword-triggered entries injected
// into the DM prompt per turn; this just chooses which files are live.
// Renders nothing when the server has no context files to offer.

import { useEffect } from "react";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

export default function ContextPicker() {
  const { state, actions } = useStore();
  const { t } = useLang();
  const manifest = state.contextManifest;

  useEffect(() => {
    if (state.status === "connected") actions.requestContextManifest();
  }, [state.status, actions]);

  const files = manifest?.files ?? [];
  if (files.length === 0) return null;

  const selected = new Set(manifest.selected ?? []);
  const toggle = (name) => {
    const next = new Set(selected);
    next.has(name) ? next.delete(name) : next.add(name);
    actions.selectContext([...next]);
  };

  return (
    <details className="panel p-3 text-sm">
      <summary className="cursor-pointer text-dungeon-gold/80">
        {t("World context")}
        {selected.size > 0 && <span className="text-dungeon-ink/50"> · {selected.size}</span>}
      </summary>
      <p className="text-xs text-dungeon-ink/50 mt-2">{t("Files the DM draws on, keyed to what's happening.")}</p>
      <ul className="mt-2 space-y-1">
        {files.map((f) => (
          <li key={f.name}>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={selected.has(f.name)} onChange={() => toggle(f.name)} />
              <span className="flex-1">{f.name}</span>
              <span className="text-[10px] uppercase text-dungeon-ink/40">
                {f.type} · {(f.size_chars / 1000).toFixed(1)}k
              </span>
            </label>
          </li>
        ))}
      </ul>
    </details>
  );
}
