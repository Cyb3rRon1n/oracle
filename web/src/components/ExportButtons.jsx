// Client-side exports: the character sheet as JSON (re-importable on the
// join screen) and the session transcript as plain text. Both are downloads
// built from store state - no server round trip, same convention the TUI's
// /export and /transcript commands established.

import { useLang } from "../i18n.jsx";
import { useStore } from "../state/store.jsx";

function download(filename, text, type = "application/json") {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const TRANSCRIPT_KINDS = {
  action: "» ",
  system: "⚠ ",
};

export default function ExportButtons() {
  const { state } = useStore();
  const { t } = useLang();
  const sheet = state.character;
  const safeName = (sheet?.name || "character").replace(/\W+/g, "_");

  function exportCharacter() {
    if (!sheet) return;
    download(`${safeName}_lv${sheet.level}.json`, JSON.stringify(sheet, null, 2));
  }

  function exportTranscript() {
    const lines = state.log
      .filter((e) => e.kind !== "system" || e.level === "warning")
      .map((e) => `${TRANSCRIPT_KINDS[e.kind] ?? ""}${e.text}`)
      .join("\n\n");
    download(`${safeName}_transcript.txt`, lines, "text/plain");
  }

  return (
    <div className="flex gap-2 text-xs">
      <button className="px-2 py-1 rounded border border-dungeon-edge hover:border-dungeon-gold transition" onClick={exportCharacter} disabled={!sheet}>
        ⬇ Character
      </button>
      <button className="px-2 py-1 rounded border border-dungeon-edge hover:border-dungeon-gold transition" onClick={exportTranscript}>
        ⬇ Transcript
      </button>
    </div>
  );
}
