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
    synced: false, // first state_sync received - until then we don't know started
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
    awaitingDM: false, // true between sending an action / starting and the DM's first word back
    inspirationArmed: false, // player asked to spend their held Inspiration on the next roll
    typing: {}, // player_id -> bool, ephemeral lobby typing indicator
    scene: null, // latest scene_update payload
    pendingProposal: null,
    contextManifest: null,
  };
}

function appendLog(log, entry) {
  const seq = ++logSeq;
  const tail = log[log.length - 1];
  if (tail && !tail.streaming && tail.kind === entry.kind && tail.text === entry.text) {
    // Reconnect replay: state_sync's log_tail then a live log_entry for the
    // same line would render the text twice. Skip the duplicate.
    return log;
  }
  if (tail && tail.streaming && entry.kind === "narration") {
    // Live DM narration streams one chunk per log_entry, done:false, closed
    // by an empty done:true chunk (server/engine.py _log_envelope) - grow
    // the open tail until that terminator lands, so a narration renders as
    // one paragraph instead of one entry per token.
    const next = [...log];
    next[next.length - 1] = { ...tail, text: tail.text + entry.text, streaming: entry.done === false };
    return next;
  }
  return [...log, { id: seq, ...entry }];
}

function reducer(state, event) {
  switch (event.type) {
    case "ws_status":
      return { ...state, status: event.status };

    case ET.STATE_SYNC: {
      const ready = new Set(event.payload.ready_players || []);
      const players = { ...state.players };
      for (const [pid, view] of Object.entries(event.payload.characters || {})) {
        players[pid] = { ...players[pid], ...view, ready: ready.has(pid) };
      }
      return {
        ...state,
        synced: true,
        started: event.payload.started,
        turnOrder: event.payload.turn_order || [],
        currentTurn: event.payload.current_turn ?? null,
        inCombat: !!event.payload.in_combat,
        // characters/npcs arrive as dicts keyed by player_id / display name
        // (server/engine.py _state_sync_envelope) - owner view for our own
        // entry, redacted public views for everyone else.
        players,
        npcs: { ...state.npcs, ...(event.payload.npcs || {}) },
        world: { ...state.world, ...(event.payload.world_state || {}) },
        log: (event.payload.log_tail || []).map((e) => ({ id: ++logSeq, ...e })),
        character: (event.payload.characters || {})[state.me] ?? state.character,
      };
    }

    case ET.LOG_ENTRY:
      return {
        ...state,
        // Any real DM output ends the "DM is thinking" state.
        awaitingDM: state.awaitingDM && event.payload.kind !== "narration",
        log: appendLog(state.log, {
          kind: event.payload.kind,
          text: event.payload.text,
          category: event.payload.category,
          done: event.payload.done,
          streaming: event.payload.kind === "narration" && event.payload.done === false,
        }),
      };

    case ET.CHARACTER_UPDATE: {
      if (event.payload.player_id !== state.me) return state;
      const character = { ...state.character, ...event.payload.sheet_delta };
      // Token spent (or lost) -> nothing left to arm.
      return { ...state, character, inspirationArmed: state.inspirationArmed && character.inspiration };
    }

    case "inspiration_armed":
      return { ...state, inspirationArmed: event.armed };

    case ET.PLAYER_UPDATE:
    case ET.PLAYER_JOINED:
      return {
        ...state,
        players: { ...state.players, [event.payload.player_id]: { ...state.players[event.payload.player_id], ...event.payload } },
      };

    case ET.PLAYER_LEFT: {
      const players = { ...state.players };
      delete players[event.payload.player_id];
      const typing = { ...state.typing };
      delete typing[event.payload.player_id];
      return { ...state, players, typing };
    }

    case ET.PRESENCE:
      return {
        ...state,
        typing: { ...state.typing, [event.payload.player_id]: !!event.payload.typing },
      };

    case ET.NPC_UPDATE:
      return { ...state, npcs: { ...state.npcs, [event.payload.name]: { ...state.npcs[event.payload.name], ...event.payload } } };

    case ET.WORLD_UPDATE:
      return { ...state, world: { ...state.world, ...event.payload } };

    case ET.TURN_PROMPT:
      return {
        ...state,
        awaitingDM: false,
        currentTurn: event.payload.player_id,
        turnOrder: event.payload.turn_order || state.turnOrder,
        inCombat: event.payload.in_combat ?? state.inCombat,
      };

    // dice_result is still emitted by the server (death saves, DM-requested
    // rolls) but the client shows those via their kind:"dice" log_entry -
    // there's no separate roller widget to feed.

    case ET.SESSION_STARTED:
      return { ...state, started: true, typing: {} };

    // Adventure wrapped: back to the tavern lobby. Keep the party, world and
    // log; App.jsx re-routes to LobbyScreen off `started`.
    case ET.SESSION_ENDED:
      return { ...state, started: false, currentTurn: null, awaitingDM: false, typing: {} };

    case ET.SYSTEM_MESSAGE:
      return {
        ...state,
        // A missed-change advisory may carry a confirmable proposal
        // ({target, hp_delta, add_condition}) - held until applied or
        // replaced by the player's next action server-side.
        pendingProposal: event.payload.proposed_change ?? state.pendingProposal,
        // An error/warning ("The DM couldn't respond", "Couldn't generate an
        // opening scene") also ends the wait.
        awaitingDM: state.awaitingDM && event.payload.level === "info",
        log: [...state.log, { id: ++logSeq, kind: "system", text: event.payload.text, level: event.payload.level }],
      };

    case "proposal_applied":
      return { ...state, pendingProposal: null };

    case "dm_pending":
      return { ...state, awaitingDM: true };

    case ET.SCENE_UPDATE:
      return { ...state, scene: event.payload };

    case ET.CONTEXT_MANIFEST:
      return { ...state, contextManifest: event.payload };

    case "context_selected":
      return state.contextManifest
        ? { ...state, contextManifest: { ...state.contextManifest, selected: event.files } }
        : state;

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
        dispatch({ type: "dm_pending" });
        connRef.current?.sendEvent(ET.PLAYER_ACTION, { text });
      },
      sendChat(text) {
        connRef.current?.sendEvent(ET.CHAT_MESSAGE, { text });
      },
      setReady(ready) {
        connRef.current?.sendEvent(ET.PLAYER_READY, { ready });
      },
      setTyping(typing) {
        connRef.current?.sendEvent(ET.SET_TYPING, { typing });
      },
      tavernRest() {
        connRef.current?.sendEvent(ET.TAVERN_REST, {});
      },
      endAdventure() {
        connRef.current?.sendEvent(ET.END_ADVENTURE, {});
      },
      editCharacter(field, value) {
        connRef.current?.sendEvent(ET.CHARACTER_EDIT, { field, value });
      },
      deathSave() {
        connRef.current?.sendEvent(ET.DEATH_SAVE, {});
      },
      toggleInspiration(armed) {
        connRef.current?.sendEvent(ET.USE_INSPIRATION, {});
        dispatch({ type: "inspiration_armed", armed });
      },
      applyProposal() {
        connRef.current?.sendEvent(ET.APPLY_PROPOSED_CHANGE, {});
        dispatch({ type: "proposal_applied" });
      },
      startAdventure() {
        dispatch({ type: "dm_pending" });
        connRef.current?.sendEvent(ET.START_SESSION, {});
      },
      requestContextManifest() {
        connRef.current?.sendEvent(ET.CONTEXT_MANIFEST_REQUEST, {});
      },
      selectContext(files) {
        connRef.current?.sendEvent(ET.CONTEXT_SELECT, { files });
        dispatch({ type: "context_selected", files }); // server sends no fresh manifest back
      },
    }),
    [],
  );

  return <StoreContext.Provider value={{ state, dispatch, actions }}>{children}</StoreContext.Provider>;
}

export function useStore() {
  return useContext(StoreContext);
}
