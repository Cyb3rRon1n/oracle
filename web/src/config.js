// Real class/race keys mirror server/engine.py's CLASS_STARTING_EQUIPMENT and
// server/rules/srd.json's races - unrecognized values fall back gracefully
// server-side, so this list is presentation, not authority.

export const CLASSES = [
  { key: "fighter", label: "Fighter" },
  { key: "wizard", label: "Wizard" },
  { key: "rogue", label: "Rogue" },
  { key: "cleric", label: "Cleric" },
];

export const RACES = [
  { key: "human", label: "Human" },
  { key: "elf", label: "Elf" },
  { key: "dwarf", label: "Dwarf" },
  { key: "halfling", label: "Halfling" },
  { group: "High Elf", key: "high_elf" },
  { group: "Wood Elf", key: "wood_elf" },
  { group: "Hill Dwarf", key: "hill_dwarf" },
  { group: "Mountain Dwarf", key: "mountain_dwarf" },
  { group: "Lightfoot Halfling", key: "lightfoot_halfling" },
  { group: "Stout Halfling", key: "stout_halfling" },
];

export const DEFAULT_SERVER = import.meta.env.VITE_SERVER_URI || "ws://localhost:8765";
