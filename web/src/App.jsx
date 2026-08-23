import JoinScreen from "./components/JoinScreen.jsx";
import GameScreen from "./components/GameScreen.jsx";
import { StoreProvider, useStore } from "./state/store.jsx";
import { LangProvider } from "./i18n.jsx";
import { loadIdentity } from "./lib/storage.js";

function Screens() {
  const { state } = useStore();
  const identity = loadIdentity();
  const hasSession = !!(identity?.sessionId && state.sessionId === identity.sessionId);
  if (!hasSession) return <JoinScreen />;
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
