import { useState } from "react";
import { LangFlags, useLang } from "../i18n.jsx";
import { CLASSES, RACES, DEFAULT_SERVER } from "../config.js";
import { newId } from "../lib/storage.js";
import { useStore } from "../state/store.jsx";

export default function JoinScreen() {
  const { actions, state } = useStore();
  const { t } = useLang();
  const [name, setName] = useState("");
  const [sessionId, setSessionId] = useState(() => newId());
  const [characterClass, setClass] = useState("fighter");
  const [race, setRace] = useState("human");
  const [imported, setImported] = useState(null);
  const [error, setError] = useState("");

  function importFile(file) {
    file
      .text()
      .then((text) => {
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== "object") throw new Error("not a character sheet");
        setImported(parsed);
        setName(parsed.name || name);
      })
      .catch((err) => setError(`${t("Import failed:")} ${err.message}`));
  }

  function submit(e) {
    e.preventDefault();
    if (!name.trim()) return setError(t("A hero needs a name."));
    try {
      // The server URI is a page-level setting; the store's connection
      // factory reads it from the module default. A custom server requires
      // VITE_SERVER_URI at build time - fine for now, revisit with a
      // settings panel if anyone actually needs two servers.
      const playerId = newId();
      const conn = actions.startSession(sessionId, playerId);
      actions.join(conn, {
        sessionId,
        playerId,
        playerName: name.trim(),
        characterClass,
        race,
        importedCharacter: imported,
      });
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <form onSubmit={submit} className="panel w-full max-w-lg p-8 space-y-5">
        <div className="flex items-center justify-between"><h1 className="text-3xl text-center flex-1">{t("Oracle")}</h1><LangFlags /></div>
        <div className="flex items-center justify-between"><p className="text-center text-dungeon-ink/70 italic flex-1">{t("An AI Dungeon Master awaits…")}</p></div>

        <label className="block">
          <span className="text-sm">{t("Character name")}</span>
          <input
            className="mt-1 w-full bg-dungeon-bg border border-dungeon-edge rounded px-3 py-2 focus:border-dungeon-gold outline-none"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("Thrain Ironveil")}
            autoFocus
          />
        </label>

        <label className="block">
          <span className="text-sm">{t("Session")}</span>
          <div className="flex gap-2 mt-1">
            <input
              className="flex-1 bg-dungeon-bg border border-dungeon-edge rounded px-3 py-2 focus:border-dungeon-gold outline-none font-mono text-xs"
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
            />
            <button type="button" className="btn-gold !px-3" onClick={() => setSessionId(newId())}>
              {t("New")}
            </button>
          </div>
          <span className="text-xs text-dungeon-ink/50">
            {t("Share this id to play together — same id, one party.")}
          </span>
        </label>

        <div className="grid grid-cols-2 gap-4">
          <label className="block">
            <span className="text-sm">{t("Class")}</span>
            <select
              className="mt-1 w-full bg-dungeon-bg border border-dungeon-edge rounded px-3 py-2"
              value={characterClass}
              onChange={(e) => setClass(e.target.value)}
            >
              {CLASSES.map((c) => (
                <option key={c.key} value={c.key}>
                  {t(c.label)}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-sm">{t("Race")}</span>
            <select
              className="mt-1 w-full bg-dungeon-bg border border-dungeon-edge rounded px-3 py-2"
              value={race}
              onChange={(e) => setRace(e.target.value)}
            >
              {RACES.map((r) => (
                <option key={r.key} value={r.key}>
                  {t(r.label ?? r.group)}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label className="block">
          <span className="text-sm">{t("Import character .json (optional)")}</span>
          <input
            type="file"
            accept=".json,application/json"
            className="mt-1 block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-dungeon-gold file:text-dungeon-bg file:px-3 file:py-1"
            onChange={(e) => e.target.files[0] && importFile(e.target.files[0])}
          />
          {imported && <span className="text-xs text-dungeon-gold">{t("Loaded:")} {imported.name}</span>}
        </label>

        {error && <p className="text-dungeon-blood text-sm">{error}</p>}

        <button className="btn-gold w-full" type="submit" disabled={state.status === "connecting"}>
          {t("Enter the adventure")}
        </button>
      </form>
    </div>
  );
}
