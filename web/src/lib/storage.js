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

export function newId() {
  return crypto.randomUUID();
}
