import { createContext, useContext, useEffect, useMemo, useReducer, useRef } from "react";
import { EventType as ET } from "../lib/protocol.js";
import { createConnection } from "../lib/ws.js";
import { loadIdentity, saveIdentity } from "../lib/storage.js";

// One reducer for every server event - the client stays a pure projection
// of server truth (the same "thin client" discipline the TUI had), so any
// state it shows can be traced to a protocol message in docs/protocol.md.

let logSeq = 0;

function initial() {
  return {
    status: "offline", // offline | connecting | connected | reconnecting
    started: false,
    sessionId: null,
    me: null,
    character: null, // my full sheet (owner view)
    players: {}, // player_id -> public view
    npcs: {}, // name -> sheet delta
    world: { location: "", summary: "", mood: "", flags: {}, objectives: [], map: { nodes: [], edges: [] }, clocks: [] },
    turnOrder: [],
    currentTurn: null,
    inCombat: false,
    log: [],
    scene: null, // latest scene_update payload
    lastRoll: null,
    pendingProposal: null,
    contextManifest: null,
  };
}

function appendLog(log, entry) {
  const seq = ++logSeq;
  if (entry.chunk) {
    // Streaming DM narration: grow the open tail entry.
    const tail = log[log.length - 1];
    if (tail && tail.streaming) {
      const next = [...log];
      next[next.length - 1] = { ...tail, text: tail.text + entry.text };
      return next;
    }
  }
  return [...log, { id: seq, ...entry }];
}

function reducer(state, event) {
  switch (event.type) {
    case "ws_status":
      return { ...state, status: event.status };

    case ET.STATE_SYNC:
      return {
        ...state,
        started: event.payload.started,
        turnOrder: event.payload.turn_order || [],
        currentTurn: event.payload.current_turn ?? null,
        inCombat: !!event.payload.in_combat,
        // characters/npcs arrive as dicts keyed by player_id / display name
        // (server/engine.py _state_sync_envelope) - owner view for our own
        // entry, redacted public views for everyone else.
        players: { ...state.players, ...(event.payload.characters || {}) },
        npcs: { ...state.npcs, ...(event.payload.npcs || {}) },
        world: { ...state.world, ...(event.payload.world_state || {}) },
        log: (event.payload.log_tail || []).map((e) => ({ id: ++logSeq, ...e })),
        character: (event.payload.characters || {})[state.me] ?? state.character,
      };

    case ET.LOG_ENTRY:
      return {
        ...state,
        log: appendLog(state.log, {
          kind: event.payload.kind,
          text: event.payload.text,
          category: event.payload.category,
          done: event.payload.done,
          streaming: event.payload.kind === "narration" && event.payload.done === false,
        }),
      };

    case ET.CHARACTER_UPDATE:
      return event.payload.player_id === state.me
        ? { ...state, character: { ...state.character, ...event.payload.sheet_delta } }
        : state;

    case ET.PLAYER_UPDATE:
    case ET.PLAYER_JOINED:
      return {
        ...state,
        players: { ...state.players, [event.payload.player_id]: { ...state.players[event.payload.player_id], ...event.payload } },
      };

    case ET.PLAYER_LEFT: {
      const players = { ...state.players };
      delete players[event.payload.player_id];
      return { ...state, players };
    }

    case ET.NPC_UPDATE:
      return { ...state, npcs: { ...state.npcs, [event.payload.name]: { ...state.npcs[event.payload.name], ...event.payload } } };

    case ET.WORLD_UPDATE:
      return { ...state, world: { ...state.world, ...event.payload } };

    case ET.TURN_PROMPT:
      return {
        ...state,
        currentTurn: event.payload.player_id,
        turnOrder: event.payload.turn_order || state.turnOrder,
        inCombat: event.payload.in_combat ?? state.inCombat,
      };

    case ET.DICE_RESULT:
      // The engine already broadcasts a kind:dice log_entry for every roll
      // (engine.py broadcasts both); this payload only drives the roller UI.
      return { ...state, lastRoll: event.payload };

    case ET.SESSION_STARTED:
      return { ...state, started: true };

    case ET.SYSTEM_MESSAGE:
      return {
        ...state,
        // A missed-change advisory may carry a confirmable proposal
        // ({target, hp_delta, add_condition}) - held until applied or
        // replaced by the player's next action server-side.
        pendingProposal: event.payload.proposed_change ?? state.pendingProposal,
        log: [...state.log, { id: ++logSeq, kind: "system", text: event.payload.text, level: event.payload.level }],
      };

    case "proposal_applied":
      return { ...state, pendingProposal: null };

    case ET.SCENE_UPDATE:
      return { ...state, scene: event.payload };

    case ET.CONTEXT_MANIFEST:
      return { ...state, contextManifest: event.payload };

    case "local_session":
      return { ...state, sessionId: event.sessionId, me: event.playerId };

    default:
      return state;
  }
}

const StoreContext = createContext(null);

export function StoreProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, null, initial);
  const connRef = useRef(null);

  useEffect(() => {
    const identity = loadIdentity();
    if (!identity?.sessionId) return; // no session yet - JoinScreen starts one
    dispatch({ type: "local_session", sessionId: identity.sessionId, playerId: identity.playerId });
    dispatch({ type: "ws_status", status: "connecting" });
    const conn = createConnection({
      sessionId: identity.sessionId,
      senderId: identity.playerId,
      onEvent: (envelope) => dispatch({ type: envelope.type, ...envelope }),
      onStatus: (status) => dispatch({ type: "ws_status", status }),
    });
    connRef.current = conn;
    // Rejoin immediately - the stored player_id lets the server resume our
    // seat and character; player_name is ignored on reconnect but must be
    // present-shaped for a genuinely new seat.
    conn.sendEvent(ET.JOIN_SESSION, { player_name: identity.playerName || "" });
    return () => conn.close();
  }, []);

  const actions = useMemo(
    () => ({
      startSession(sessionId, playerId) {
        saveIdentity({ sessionId, playerId });
        dispatch({ type: "local_session", sessionId, playerId });
        dispatch({ type: "ws_status", status: "connecting" });
        const conn = createConnection({
          sessionId,
          senderId: playerId,
          onEvent: (envelope) => dispatch({ type: envelope.type, ...envelope }),
          onStatus: (status) => dispatch({ type: "ws_status", status }),
        });
        connRef.current = conn;
        return conn;
      },
      join(conn, { sessionId, playerId, playerName, characterClass, race, importedCharacter }) {
        saveIdentity({ sessionId, playerId, playerName });
        conn.sendEvent(
          ET.JOIN_SESSION,
          {
            player_name: playerName,
            ...(characterClass ? { character_class: characterClass } : {}),
            ...(race ? { race } : {}),
            ...(importedCharacter ? { imported_character: importedCharacter } : {}),
          },
          playerId,
        );
        dispatch({ type: "ws_status", status: "connected" });
      },
      sendAction(text) {
        connRef.current?.sendEvent(ET.PLAYER_ACTION, { text });
      },
      sendChat(text) {
        connRef.current?.sendEvent(ET.CHAT_MESSAGE, { text });
      },
      rollDice(dice, reason) {
        connRef.current?.sendEvent(ET.DICE_ROLL, { dice, reason });
      },
      editCharacter(field, value) {
        connRef.current?.sendEvent(ET.CHARACTER_EDIT, { field, value });
      },
      deathSave() {
        connRef.current?.sendEvent(ET.DEATH_SAVE, {});
      },
      applyProposal() {
        connRef.current?.sendEvent(ET.APPLY_PROPOSED_CHANGE, {});
        dispatch({ type: "proposal_applied" });
      },
      startAdventure() {
        connRef.current?.sendEvent(ET.START_SESSION, {});
      },
      requestContextManifest() {
        connRef.current?.sendEvent(ET.CONTEXT_MANIFEST_REQUEST, {});
      },
      selectContext(files) {
        connRef.current?.sendEvent(ET.CONTEXT_SELECT, { files });
      },
    }),
    [],
  );

  return <StoreContext.Provider value={{ state, dispatch, actions }}>{children}</StoreContext.Provider>;
}

export function useStore() {
  return useContext(StoreContext);
}
