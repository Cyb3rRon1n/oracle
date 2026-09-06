// Persistent client identity - a browser tab keeps its player_id across
// reconnects and reloads so the server can resume its seat/character.

const KEY = "oracle_identity";

export function loadIdentity() {
  try {
    return JSON.parse(localStorage.getItem(KEY)) || null;
  } catch {
    return null;
  }
}

export function saveIdentity(identity) {
  localStorage.setItem(KEY, JSON.stringify(identity));
}

export function clearIdentity() {
  localStorage.removeItem(KEY);
}

// The game server's websocket URL. VITE_SERVER_URI is baked in at build
// time; this lets a player point the same built page at their own server
// without a rebuild - the #1 setup failure in docs/walkthrough.md.
const SERVER_KEY = "oracle_server";
const DEFAULT_SERVER_URI = import.meta.env.VITE_SERVER_URI || "ws://localhost:8765";

export function loadServer() {
  try {
    return localStorage.getItem(SERVER_KEY) || DEFAULT_SERVER_URI;
  } catch {
    return DEFAULT_SERVER_URI;
  }
}

export function saveServer(uri) {
  const trimmed = (uri || "").trim();
  if (!trimmed || trimmed === DEFAULT_SERVER_URI) localStorage.removeItem(SERVER_KEY);
  else localStorage.setItem(SERVER_KEY, trimmed);
}

export function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // crypto.randomUUID needs a secure context (https/localhost) — LAN/Tailscale http isn't one
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  return [...b]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("")
    .replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/, "$1-$2-$3-$4-$5");
}
