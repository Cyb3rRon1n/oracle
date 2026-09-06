import JoinScreen from "./components/JoinScreen.jsx";
import LobbyScreen from "./components/LobbyScreen.jsx";
import GameScreen from "./components/GameScreen.jsx";
import { StoreProvider, useStore } from "./state/store.jsx";
import { LangProvider } from "./i18n.jsx";
import { loadIdentity } from "./lib/storage.js";

function Screens() {
  const { state } = useStore();
  const identity = loadIdentity();
  const hasSession = !!(identity?.sessionId && state.sessionId === identity.sessionId);
  if (!hasSession) return <JoinScreen />;
  // Until the first state_sync we don't know whether the adventure has started -
  // show a quiet wait rather than flash the wrong screen.
  if (!state.synced) {
    return (
      <div className="min-h-screen flex items-center justify-center text-dungeon-ink/50 italic">
        Entering the tavern…
      </div>
    );
  }
  if (!state.started) return <LobbyScreen />;
  return <GameScreen />;
}

export default function App() {
  return (
    <LangProvider>
      <StoreProvider>
        <Screens />
      </StoreProvider>
    </LangProvider>
  );
}
