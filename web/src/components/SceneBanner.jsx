// A generated establishing-shot banner for the party's current location -
// player-triggered only (generate_scene), same "always asks first" stance
// as Avatar.jsx's own portrait generation. Cleared server-side whenever
// WorldState.location changes, so this only ever shows a match for where
// the party actually is, or nothing at all until someone clicks generate.

import { useState } from "react";
import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";
import { STYLE_OPTIONS } from "./Avatar.jsx";

export default function SceneBanner() {
  const [style, setStyle] = useState("fantasy");
  const { state, actions } = useStore();
  const { t } = useLang();
  const image = state.world.scene_image;
  const pending = state.scenePending;
  const pct = state.sceneProgress?.total
    ? Math.round((state.sceneProgress.step / state.sceneProgress.total) * 100)
    : null;

  if (image) {
    return (
      <div className="relative rounded overflow-hidden border border-dungeon-edge">
        <img src={image} alt="" className="w-full h-40 object-cover" />
        <button
          onClick={() => actions.generateScene(style)}
          disabled={pending}
          className="absolute bottom-1 right-1 text-[10px] px-1.5 py-0.5 rounded bg-black/50 text-dungeon-ink/70 hover:text-dungeon-ink border border-dungeon-edge"
        >
          {pending ? (pct != null ? `${pct}%` : t("Generating…")) : t("Regenerate")}
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      {pending ? (
        <span className="text-dungeon-ink/50">{pct != null ? `${t("Generating…")} ${pct}%` : t("Generating…")}</span>
      ) : (
        <>
          <select
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            className="text-[10px] bg-dungeon-bg border border-dungeon-edge rounded px-1 py-0.5 text-dungeon-ink/70"
          >
            {STYLE_OPTIONS.map(([key, label]) => (
              <option key={key} value={key}>
                {t(label)}
              </option>
            ))}
          </select>
          <button
            onClick={() => actions.generateScene(style)}
            className="text-dungeon-ink/50 hover:text-dungeon-ink underline"
          >
            {t("Generate scene")}
          </button>
        </>
      )}
    </div>
  );
}
