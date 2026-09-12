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
  "Copy": "Copier",
  "Copied": "Copié",
  "Server address": "Adresse du serveur",
  "Point this page at your own server — e.g. ws://192.168.1.10:8765.": "Dirigez cette page vers votre propre serveur — p. ex. ws://192.168.1.10:8765.",
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
  "The Tavern": "La Taverne",
  "When last we met…": "La dernière fois…",
  "Still to do:": "Reste à faire :",
  "The party": "Le groupe",
  "Tavern talk": "Discussion de taverne",
  "The fire crackles. Someone should say something.": "Le feu crépite. Quelqu'un devrait dire quelque chose.",
  "Say something, or /me does something…": "Dites quelque chose, ou /me fait quelque chose…",
  "Review my character": "Consulter mon personnage",
  "The quest board": "Le tableau des quêtes",
  "New hooks": "Nouvelles pistes",
  "The road is open — start when ready.": "La route est ouverte — partez quand vous voulez.",
  "Return to tavern": "Retour à la taverne",
  "Confirm — end adventure": "Confirmer — fin de l'aventure",
  "Wrap up the adventure and return to the tavern": "Conclure l'aventure et retourner à la taverne",
  "Long rest": "Repos long",
  "Full HP, half your hit dice back, spell slots refilled": "PV au max, moitié des dés de vie, emplacements de sorts rechargés",
  "Ready": "Prêt",
  "I'm ready": "Je suis prêt",
  "Not ready": "Pas prêt",
  "Start now": "Commencer maintenant",
  "Begin now without waiting for everyone": "Commencer sans attendre tout le monde",
  "ready": "prêt",
  "not ready": "pas prêt",
  "typing…": "écrit…",
  "dying": "mourant",
  "The DM is weaving the scene…": "Le MJ tisse la scène…",
  "World context": "Contexte du monde",
  "Files the DM draws on, keyed to what's happening.": "Fichiers que le MJ utilise, selon ce qui se passe.",
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
  "Hide": "Masquer",
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
  "Equipped": "Équipé",
  "Generate portrait": "Générer un portrait",
  "Regenerate": "Régénérer",
  "Generating…": "Génération…",
  "Fantasy painting": "Peinture fantastique",
  "Anime": "Anime",
  "Comic book": "Bande dessinée",
  "Realistic photo": "Photo réaliste",
  "equip": "équiper",
  "unequip": "retirer",
  "use": "utiliser",
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

  // Full character sheet
  "Sheet": "Fiche",
  "Close": "Fermer",
  "planned": "prévu",
  "Alignment": "Alignement",
  "Proficiency bonus": "Bonus de maîtrise",
  "Saving throws": "Jets de sauvegarde",
  "Passive perception": "Perception passive",
  "Passive investigation": "Investigation passive",
  "Passive insight": "Perspicacité passive",
  "ft": "ft",
  "Armor class": "Classe d'armure",
  "Initiative": "Initiative",
  "Speed": "Vitesse",
  "Hit points": "Points de vie",
  "Temp HP": "PV temporaires",
  "Hit dice": "Dés de vie",
  "Death saves": "Jets contre la mort",
  "Successes": "Réussites",
  "Failures": "Échecs",
  "Attacks": "Attaques",
  "No attacks.": "Aucune attaque.",
  "Name": "Nom",
  "Atk": "Att.",
  "Damage": "Dégâts",
  "Currency": "Argent",
  "Gold": "Or",
  "Inspiration": "Inspiration",
  "Use on next roll": "Utiliser au prochain jet",
  "Armed — next roll": "Prête — prochain jet",
  "None yet.": "Aucune pour l'instant.",
  "Origin": "Origine",
  "Personality": "Personnalité",
  "Traits": "Traits",
  "Ideals": "Idéaux",
  "Bonds": "Liens",
  "Flaws": "Défauts",
  "Proficiencies & languages": "Maîtrises et langues",
  "Armor": "Armures",
  "Weapons": "Armes",
  "Tools": "Outils",
  "Languages": "Langues",
  "none": "aucune",
  "Spellcasting": "Incantation",
  "Not a spellcaster.": "Pas de lanceur de sorts.",
  "Save DC": "DD de sauvegarde",
  "Attack": "Attaque",

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
