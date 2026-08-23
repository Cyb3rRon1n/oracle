// Mirrors shared/protocol.py's EventType union and Envelope shape.
// Keep both sides in sync - the schema freeze lives in docs/protocol.md.

export const EventType = {
  // client -> server
  JOIN_SESSION: "join_session",
  PLAYER_ACTION: "player_action",
  CHAT_MESSAGE: "chat_message",
  CHARACTER_EDIT: "character_edit",
  DICE_ROLL: "dice_roll",
  DEATH_SAVE: "death_save",
  RECONNECT: "reconnect",
  START_SESSION: "start_session",
  START_COMBAT: "start_combat",
  END_COMBAT: "end_combat",

  // protocol v2 (docs/protocol.md "Protocol v2 additions")
  CONTEXT_MANIFEST_REQUEST: "context_manifest_request",
  CONTEXT_SELECT: "context_select",

  // server -> client
  STATE_SYNC: "state_sync",
  LOG_ENTRY: "log_entry",
  CHARACTER_UPDATE: "character_update",
  PLAYER_UPDATE: "player_update",
  NPC_UPDATE: "npc_update",
  WORLD_UPDATE: "world_update",
  TURN_PROMPT: "turn_prompt",
  DICE_RESULT: "dice_result",
  PLAYER_JOINED: "player_joined",
  PLAYER_LEFT: "player_left",
  SESSION_STARTED: "session_started",
  SYSTEM_MESSAGE: "system_message",
  APPLY_PROPOSED_CHANGE: "apply_proposed_change",
  CONTEXT_MANIFEST: "context_manifest",
  SCENE_UPDATE: "scene_update",
};

export function makeEnvelope(type, sessionId, senderId, payload = {}) {
  return { type, session_id: sessionId, sender_id: senderId, ts: new Date().toISOString(), payload };
}

export function parseEnvelope(raw) {
  const env = JSON.parse(raw);
  return env;
}
