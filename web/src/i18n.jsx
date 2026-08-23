// Minimal i18n (OpenArcana-pattern): English source strings are their own
// keys; the French dict overrides them. Language persists in localStorage
// and flips live via the header flags - components just wrap literals in
// t(...) and never need key names.

import { createContext, useContext, useState } from "react";

const FR = {
  // Join screen
  "Oracle": "Oracle",
  "An AI Dungeon Master awaits…": "Un Maître du Donjon IA vous attend…",
  "Character name": "Nom du personnage",
  "Thrain Ironveil": "Thrain Voiledefer",
  "Session": "Session",
  "New": "Nouvelle",
  "Share this id to play together — same id, one party.": "Partagez cet identifiant pour jouer ensemble — même identifiant, un seul groupe.",
  "Class": "Classe",
  "Fighter": "Guerrier",
  "Wizard": "Magicien",
  "Rogue": "Roublard",
  "Cleric": "Clerc",
  "Race": "Race",
  "Human": "Humain",
  "Elf": "Elfe",
  "Dwarf": "Nain",
  "Halfling": "Halfelin",
  "High Elf": "Haut elfe",
  "Wood Elf": "Elfe des bois",
  "Hill Dwarf": "Nain des collines",
  "Mountain Dwarf": "Nain des montagnes",
  "Lightfoot Halfling": "Halfelin pieds-légers",
  "Stout Halfling": "Halfelin robuste",
  "Import character .json (optional)": "Importer un personnage .json (optionnel)",
  "A hero needs a name.": "Un héros a besoin d'un nom.",
  "Import failed:": "Échec de l'import :",
  "Enter the adventure": "Entrer dans l'aventure",

  // Game screen
  "The party has gathered.": "Le groupe est réuni.",
  "Begin the adventure": "Commencer l'aventure",
  "Send": "Envoyer",
  ", what do you do?": ", que faites-vous ?",
  "It's not your turn — chat out of character…": "Ce n'est pas votre tour — discussion hors jeu…",
  "Wait for the adventure to begin…": "Attendez le début de l'aventure…",
  "Turn: ": "Tour : ",
  " yours": " le vôtre",
  "The DM suggests an unrecorded change:": "Le MJ suggère un changement non enregistré :",
  "HP": "PV",
  "— apply it?": "— l'appliquer ?",
  "Apply": "Appliquer",
  "connected": "connecté",
  "reconnecting": "reconnexion…",

  // Sheet tabs
  "Overview": "Résumé",
  "Map": "Carte",
  "Abilities": "Caractéristiques",
  "Inventory": "Inventaire",
  "Spells": "Sorts",
  "Notes": "Notes",
  "Lv": "Niv",
  "Loaded:": "Chargé :",
  "No character yet.": "Pas encore de personnage.",
  "adventurer": "aventurier",
  "You are dying": "Vous êtes mourant",
  "Roll a death save": "Jet de sauvegarde contre la mort",
  "SLAIN": "TUE",
  "Origin:": "Origine :",
  "Proficiencies": "Maîtrises",
  "Empty pockets.": "Poches vides.",
  "Add an item…": "Ajouter un objet…",
  "Add": "Ajouter",
  "equip": "équiper",
  "unequip": "retirer",
  "drop": "jeter",
  "weapon": "arme",
  "armor": "armure",
  "shield": "bouclier",
  "No spellcasting.": "Aucun sort.",
  "Spell save DC": "DD de sauvegarde des sorts",
  "Class features": "Capacités de classe",
  "Racial traits": "Traits raciaux",
  "Your private journal…": "Votre journal privé…",
  "Save notes": "Enregistrer les notes",

  // Scene panel
  "Objectives": "Objectifs",
  "Present": "Présents",
  "Of interest": "À noter",
  "You might…": "Vous pourriez…",
  "Clocks": "Horloges",
  "Examine": "Examiner",

  // Map
  "The map has not been charted yet.": "La carte n'a pas encore été dressée.",
  "You are at": "Vous êtes à",
  "an unknown place": "un endroit inconnu",

  // Exports
  "⬇ Character": "⬇ Personnage",
  "⬇ Transcript": "⬇ Transcription",
};

const DICTS = { en: {}, fr: FR };

const LangCtx = createContext({ lang: "en", setLang: () => {}, t: (s) => s });

export function LangProvider({ children }) {
  const [lang, setLang] = useState(() => localStorage.getItem("oracle_lang") || "en");
  const t = (s) => DICTS[lang]?.[s] ?? s;
  const value = {
    lang,
    setLang: (l) => {
      localStorage.setItem("oracle_lang", l);
      setLang(l);
    },
    t,
  };
  return <LangCtx.Provider value={value}>{children}</LangCtx.Provider>;
}

export function useLang() {
  return useContext(LangCtx);
}

export function LangFlags({ className = "" }) {
  const { lang, setLang } = useLang();
  return (
    <span className={`flex gap-1 ${className}`}>
      <FlagButton active={lang === "fr"} onClick={() => setLang("fr")} title="Français">
        🇫🇷
      </FlagButton>
      <FlagButton active={lang === "en"} onClick={() => setLang("en")} title="English">
        🇬🇧
      </FlagButton>
    </span>
  );
}

function FlagButton({ active, onClick, title, children }) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`text-base leading-none rounded transition ${active ? "opacity-100 ring-1 ring-dungeon-gold" : "opacity-40 hover:opacity-80"}`}
    >
      {children}
    </button>
  );
}
