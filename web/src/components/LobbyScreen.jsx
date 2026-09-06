import { useEffect, useRef, useState } from "react";
import CharacterSheetFull from "./CharacterSheetFull.jsx";
import ContextPicker from "./ContextPicker.jsx";
import { LangFlags, useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

// The tavern: where the party gathers before (and between) adventures - chat,
// review characters, catch up on where you left off, and ready up. Shown while
// connected but !state.started; the server auto-starts once everyone's ready.

export default function LobbyScreen() {
  const { state, actions } = useStore();
  const { t } = useLang();
  const [sheetOpen, setSheetOpen] = useState(false);

  const roster = Object.values(state.players);
  const me = state.players[state.me];
  const iAmReady = !!me?.ready;
  const readyCount = roster.filter((p) => p.ready).length;
  const objectives = (state.world.objectives || []).filter((o) => o.status === "active");
  const hasRecap = !!state.world.summary || objectives.length > 0;

  return (
    <div className="min-h-screen p-4 sm:p-6">
      <div className="max-w-3xl mx-auto space-y-4">
        <div className="panel px-4 py-3 flex items-center justify-between">
          <div>
            <div className="font-display text-2xl text-dungeon-gold">{t("The Tavern")}</div>
            <div className="text-xs text-dungeon-ink/50 font-mono">{state.sessionId}</div>
          </div>
          <LangFlags />
        </div>

        {hasRecap && (
          <div className="panel p-4 border-dungeon-gold/40 space-y-2">
            <div className="text-xs uppercase tracking-widest text-dungeon-gold/70">{t("When last we met…")}</div>
            {state.world.summary && (
              <p className="text-sm italic text-dungeon-ink/80">{state.world.summary}</p>
            )}
            {objectives.length > 0 && (
              <div className="text-sm">
                <span className="text-dungeon-ink/50">{t("Still to do:")}</span>{" "}
                {objectives.map((o) => o.text).join(" · ")}
              </div>
            )}
            <div className="text-xs text-dungeon-ink/50">
              {roster.map((p) => `${p.name} ${p.hp}/${p.max_hp}`).join("   ")}
            </div>
          </div>
        )}

        <div className="grid gap-4 md:grid-cols-[1fr_1.4fr]">
          <Roster roster={roster} me={state.me} typing={state.typing} t={t} />
          <TavernChat state={state} actions={actions} t={t} />
        </div>

        <ContextPicker />

        <div className="panel p-4 flex flex-wrap items-center gap-3">
          <button
            className="px-3 py-1.5 rounded border border-dungeon-edge hover:border-dungeon-gold text-sm"
            onClick={() => setSheetOpen(true)}
            disabled={!state.character}
          >
            📜 {t("Review my character")}
          </button>
          <button
            className="px-3 py-1.5 rounded border border-dungeon-edge hover:border-dungeon-gold text-sm"
            onClick={actions.tavernRest}
            title={t("Full HP, half your hit dice back, spell slots refilled")}
          >
            🛏 {t("Long rest")}
          </button>
          <div className="flex-1" />
          <span className="text-sm text-dungeon-ink/60">
            {t("Ready")}: <b className="text-dungeon-ink">{readyCount}</b> / {roster.length}
          </span>
          <button
            className={`btn-gold ${iAmReady ? "opacity-60" : ""}`}
            onClick={() => actions.setReady(!iAmReady)}
          >
            {iAmReady ? t("Not ready") : t("I'm ready")}
          </button>
          <button
            className="px-3 py-1.5 rounded border border-dungeon-edge hover:border-dungeon-gold text-sm"
            onClick={actions.startAdventure}
            title={t("Begin now without waiting for everyone")}
          >
            {t("Start now")}
          </button>
        </div>
      </div>

      {sheetOpen && <CharacterSheetFull onClose={() => setSheetOpen(false)} />}
    </div>
  );
}

function Roster({ roster, me, typing, t }) {
  const [open, setOpen] = useState(null);
  return (
    <div className="panel p-3">
      <div className="text-xs uppercase tracking-widest text-dungeon-gold/70 mb-2">{t("The party")}</div>
      <ul className="space-y-1">
        {roster.map((p) => {
          const descr = [p.race, p.character_class].filter(Boolean).join(" ");
          return (
            <li key={p.player_id}>
              <button
                className="w-full text-left flex items-center gap-2 text-sm py-1 hover:text-dungeon-gold"
                onClick={() => setOpen(open === p.player_id ? null : p.player_id)}
              >
                <span
                  className={`w-2 h-2 rounded-full shrink-0 ${p.ready ? "bg-emerald-500" : "bg-dungeon-edge"}`}
                  title={p.ready ? t("ready") : t("not ready")}
                />
                <span className={p.player_id === me ? "text-dungeon-gold" : ""}>{p.name}</span>
                <span className="text-dungeon-ink/40 text-xs">{descr}</span>
                {typing[p.player_id] && <span className="text-dungeon-ink/40 text-xs italic">{t("typing…")}</span>}
              </button>
              {open === p.player_id && (
                <div className="ml-4 mb-1 text-xs text-dungeon-ink/60">
                  {t("Lv")} {p.level} · HP {p.hp}/{p.max_hp} · AC {p.ac}
                  {p.conditions?.length ? ` · ${p.conditions.join(", ")}` : ""}
                  {p.dead ? ` · ${t("SLAIN")}` : p.dying ? ` · ${t("dying")}` : ""}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function TavernChat({ state, actions, t }) {
  const [draft, setDraft] = useState("");
  const endRef = useRef(null);
  const lastTyping = useRef(0);
  const lines = state.log.filter((e) => e.kind === "chat" || e.kind === "system" || e.kind === "keeper");

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines.length]);

  function onChange(e) {
    setDraft(e.target.value);
    const now = Date.now();
    if (e.target.value && now - lastTyping.current > 2000) {
      lastTyping.current = now;
      actions.setTyping(true);
    }
  }
  function submit(e) {
    e.preventDefault();
    const text = draft.trim();
    if (!text) return;
    actions.sendChat(text);
    setDraft("");
    lastTyping.current = 0;
    actions.setTyping(false);
  }

  return (
    <div className="panel p-3 flex flex-col min-h-[16rem]">
      <div className="text-xs uppercase tracking-widest text-dungeon-gold/70 mb-2">{t("Tavern talk")}</div>
      <div className="flex-1 overflow-y-auto space-y-1 text-sm max-h-64">
        {lines.length === 0 && <p className="text-dungeon-ink/40 italic">{t("The fire crackles. Someone should say something.")}</p>}
        {lines.map((e) => (
          <p key={e.id} className={LINE_STYLE[e.kind] || "text-purple-300"}>
            {e.kind === "system" ? `⚠ ${e.text}` : e.kind === "keeper" ? `🍺 ${e.text}` : renderChat(e.text)}
          </p>
        ))}
        <div ref={endRef} />
      </div>
      <form onSubmit={submit} className="mt-2 flex gap-2">
        <input
          className="flex-1 bg-dungeon-bg border border-dungeon-edge rounded px-2 py-1.5 text-sm focus:border-dungeon-gold outline-none"
          value={draft}
          onChange={onChange}
          onBlur={() => actions.setTyping(false)}
          placeholder={t("Say something, or /me does something…")}
        />
        <button className="btn-gold !px-3 text-sm" type="submit">{t("Send")}</button>
      </form>
    </div>
  );
}

const LINE_STYLE = {
  system: "text-dungeon-blood/80",
  keeper: "text-dungeon-gold/90 italic",
  chat: "text-purple-300",
};

function renderChat(text) {
  if (text.startsWith("/me ")) {
    return <span className="italic text-dungeon-gold/80">* {text.slice(4)}</span>;
  }
  return text;
}
