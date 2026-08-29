import { useEffect, useRef, useState } from "react";
import CharacterSheet from "./CharacterSheet.jsx";
import DiceTray from "./DiceTray.jsx";
import ExportButtons from "./ExportButtons.jsx";
import MapPanel from "./MapPanel.jsx";
import ScenePanel from "./ScenePanel.jsx";
import { LangFlags, useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

// Phase-3 shell: the shared narration log (streaming), presence strip, and
// the action input. Sheet tabs, scene chips, clocks, map and dice arrive in
// phases 4/5 on top of the same store.

const LOG_STYLES = {
  narration: "text-dungeon-ink",
  action: "text-dungeon-gold/90 italic",
  dice: "text-sky-300",
  outcome: "text-emerald-300",
  chat: "text-purple-300",
  system: "text-dungeon-blood/90",
};

function ConnectionBadge({ status }) {
  const { t } = useLang();
  const color =
    status === "connected" ? "bg-emerald-500" : status === "reconnecting" ? "bg-dungeon-blood animate-pulse" : "bg-dungeon-edge";
  return (
    <span className="flex items-center gap-1.5 text-dungeon-ink/60">
      <span className={`w-2 h-2 rounded-full ${color}`} /> {t(status)}
    </span>
  );
}

export default function GameScreen() {
  const { state, actions } = useStore();
  const { t } = useLang();
  const [draft, setDraft] = useState("");
  const [showMap, setShowMap] = useState(true);
  const logEndRef = useRef(null);

  const isMyTurn = state.currentTurn && state.currentTurn === state.me;
  const myName = state.players[state.me]?.name;

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.log]);

  function submit(e) {
    e.preventDefault();
    if (!draft.trim()) return;
    if (isMyTurn) actions.sendAction(draft.trim());
    else actions.sendChat(draft.trim());
    setDraft("");
  }

  return (
    <div className="min-h-screen flex flex-col p-4 gap-3 max-w-5xl mx-auto">
      <header className="flex items-center justify-between panel px-4 py-2">
        <h1 className="text-lg">{t("Oracle")}</h1>
        <div className="flex items-center gap-3 text-sm">
          <ExportButtons />
          <LangFlags />
          <Presence players={state.players} me={state.me} />
          <ConnectionBadge status={state.status} />
        </div>
      </header>

      {state.pendingProposal && (
        <div className="panel border-dungeon-gold/50 p-3 flex items-center justify-between gap-3 text-sm">
          <span>
{t("The DM suggests an unrecorded change:")}
            <b>
              {[state.pendingProposal.hp_delta ? `${state.pendingProposal.hp_delta > 0 ? "+" : ""}${state.pendingProposal.hp_delta} HP` : null,
                state.pendingProposal.add_condition]
                .filter(Boolean)
                .join(", ")}
            </b>{" "}
            {t("— apply it?")}
          </span>
          <button className="btn-gold !py-1 text-xs" onClick={actions.applyProposal}>
            {t("Apply")}
          </button>
        </div>
      )}

      {!state.started ? (
        <div className="panel p-6 text-center space-y-3">
          <p className="italic text-dungeon-ink/70">{t("The party has gathered.")}</p>
          <button className="btn-gold" onClick={actions.startAdventure}>
            {t("Begin the adventure")}
          </button>
        </div>
      ) : null}

      <main className="panel flex-1 min-h-[40vh] overflow-y-auto p-4 space-y-2">
        {state.log.map((entry) => (
          <LogLine key={entry.id} entry={entry} />
        ))}
        <div ref={logEndRef} />
      </main>

      <div className="grid grid-cols-[1fr_320px] gap-3 items-start max-h-[38vh] overflow-hidden">
        <ScenePanel onSuggest={(text) => setDraft(text)} />
        <CharacterSheet />
      </div>

      <div className="panel px-3 py-2">
        <DiceTray />
      </div>

      <form onSubmit={submit} className="panel p-3 flex gap-2">
        <input
          className="flex-1 bg-dungeon-bg border border-dungeon-edge rounded px-3 py-2 focus:border-dungeon-gold outline-none"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={
            !state.started
              ? t("Wait for the adventure to begin…")
              : isMyTurn
                ? `${myName ?? ""}${t(", what do you do?")}`
                : t("It's not your turn — chat out of character…")
          }
          autoFocus
        />
        <button className="btn-gold" type="submit">
          {t("Send")}
        </button>
      </form>

      {showMap ? (
        <div className="fixed bottom-4 right-4 z-30 w-64 panel p-2 shadow-lg">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-display tracking-wide text-dungeon-gold">{t("Map")}</span>
            <button
              className="text-xs text-dungeon-ink/60 hover:text-dungeon-ink"
              onClick={() => setShowMap(false)}
            >
              {t("Hide")}
            </button>
          </div>
          <MapPanel />
        </div>
      ) : (
        <button
          className="fixed bottom-4 right-4 z-30 btn-gold !py-1 !px-2 text-xs shadow-lg"
          onClick={() => setShowMap(true)}
        >
          🗺 {t("Map")}
        </button>
      )}

    </div>
  );
}

function LogLine({ entry }) {
  const style = LOG_STYLES[entry.kind] || "";
  if (entry.kind === "system") {
    return (
      <p className={`${style} border-l-2 border-dungeon-blood/60 pl-2`}>
        ⚠ {entry.text}
      </p>
    );
  }
  return (
    <p className={style}>
      {entry.kind === "action" ? `» ${entry.text}` : entry.text}
    </p>
  );
}

function Presence({ players, me }) {
  return (
    <span className="flex -space-x-1">
      {Object.values(players).map((p) => (
        <span
          key={p.player_id}
          title={p.name}
          className={`w-7 h-7 rounded-full border text-xs flex items-center justify-center font-display ${
            p.player_id === me ? "border-dungeon-gold text-dungeon-gold" : "border-dungeon-edge"
          } ${p.dead ? "opacity-30" : ""}`}
        >
          {(p.name || "?").slice(0, 2)}
        </span>
      ))}
    </span>
  );
}
