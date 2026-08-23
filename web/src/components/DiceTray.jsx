// Dice tray: player-initiated rolls. Every button sends dice_roll; the
// result comes back as a broadcast dice_result (the log line itself arrives
// separately as a kind:dice log_entry). Rolls execute server-side - the
// tray is a remote control, not the dice.

import { useState } from "react";
import { useStore } from "../state/store.jsx";

const DICE = [4, 6, 8, 10, 12, 20];

export default function DiceTray() {
  const { state, actions } = useStore();
  const [flash, setFlash] = useState(null);
  const last = state.lastRoll;

  function roll(sides) {
    actions.rollDice(`1d${sides}`, `player d${sides} roll`);
    setFlash(`1d${sides}`);
    setTimeout(() => setFlash(null), 900);
  }

  const isMine = last && flash && last.dice === flash;

  return (
    <div className="flex items-center gap-1.5">
      {DICE.map((sides) => (
        <button
          key={sides}
          onClick={() => roll(sides)}
          title={`Roll 1d${sides}`}
          className="w-9 h-9 rounded-full border border-dungeon-gold/50 text-dungeon-gold font-display text-xs hover:bg-dungeon-gold hover:text-dungeon-bg transition"
        >
          d{sides}
        </button>
      ))}
      {last && (
        <span
          className={`ml-1 text-sm tabular-nums transition ${isMine ? "text-dungeon-gold scale-125" : "text-dungeon-ink/60"}`}
          title={last.purpose || ""}
        >
          {isMine ? "rolling…" : `${last.dice}: ${last.result}${last.success == null ? "" : last.success ? " ✓" : " ✗"}`}
        </span>
      )}
    </div>
  );
}
