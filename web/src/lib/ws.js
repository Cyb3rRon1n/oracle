import { makeEnvelope, parseEnvelope } from "./protocol.js";

const SERVER_URI = import.meta.env.VITE_SERVER_URI || "ws://localhost:8765";

// Reconnect backoff - starts at 1s, doubles to a 10s ceiling. The server
// resumes seats from the join envelope's player_id, so every attempt is a
// plain join_session carrying the stored identity.
function backoffDelay(attempt) {
  return Math.min(1000 * 2 ** attempt, 10000);
}

export function createConnection({ sessionId, senderId, onEvent, onStatus }) {
  let ws = null;
  let attempt = 0;
  let closedByUser = false;
  let pending = [];

  function connect() {
    if (closedByUser) return;
    ws = new WebSocket(SERVER_URI);
    ws.onopen = () => {
      attempt = 0;
      onStatus?.("connected");
      for (const env of pending.splice(0)) send(env);
    };
    ws.onmessage = (msg) => {
      try {
        onEvent(parseEnvelope(msg.data));
      } catch (err) {
        console.error("bad envelope", err);
      }
    };
    ws.onclose = () => {
      if (closedByUser) return;
      onStatus?.("reconnecting");
      setTimeout(connect, backoffDelay(attempt++));
    };
    ws.onerror = () => ws.close();
  }

  // Queue anything sent while the socket is down; flush in order on open.
  function send(envelope) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(envelope));
    } else {
      pending.push(envelope);
    }
  }

  function sendEvent(type, payload = {}, sender = senderId) {
    send(makeEnvelope(type, sessionId, sender, payload));
  }

  connect();

  return {
    sendEvent,
    close() {
      closedByUser = true;
      ws?.close();
    },
  };
}
