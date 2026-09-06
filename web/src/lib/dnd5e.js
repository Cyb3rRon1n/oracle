// Fixed 5e reference data the character sheet needs and the server doesn't
// send because it never changes: which ability governs each skill, the
// display order of abilities, and the passive-perception formula. This is
// universal game structure (facts, not SRD prose) - the server sends the
// character-specific parts (which skills/saves are proficient, the scores).

export const ABILITIES = ["str", "dex", "con", "int", "wis", "cha"];

export const ABILITY_LABEL = {
  str: "Strength",
  dex: "Dexterity",
  con: "Constitution",
  int: "Intelligence",
  wis: "Wisdom",
  cha: "Charisma",
};

// All 18 skills in the order the paper sheet lists them, each with its
// governing ability. Matches server/state.py's SKILL_ABILITIES.
export const SKILLS = [
  ["acrobatics", "dex"],
  ["animal_handling", "wis"],
  ["arcana", "int"],
  ["athletics", "str"],
  ["deception", "cha"],
  ["history", "int"],
  ["insight", "wis"],
  ["intimidation", "cha"],
  ["investigation", "int"],
  ["medicine", "wis"],
  ["nature", "int"],
  ["perception", "wis"],
  ["performance", "cha"],
  ["persuasion", "cha"],
  ["religion", "int"],
  ["sleight_of_hand", "dex"],
  ["stealth", "dex"],
  ["survival", "wis"],
];

export const skillLabel = (key) =>
  key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const fmtMod = (n) => (n >= 0 ? `+${n}` : `${n}`);

const SKILL_ABILITY = Object.fromEntries(SKILLS);

// 10 + the skill's ability modifier + proficiency bonus if proficient in it.
export function passiveScore(sheet, skill) {
  const ability = SKILL_ABILITY[skill] ?? "wis";
  const mod = sheet.stat_modifiers?.[ability] ?? 0;
  const prof = (sheet.skill_proficiencies || []).includes(skill)
    ? sheet.proficiency_bonus ?? 2
    : 0;
  return 10 + mod + prof;
}

export const passivePerception = (sheet) => passiveScore(sheet, "perception");
