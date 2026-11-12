import { Navigate, Route, Routes } from "react-router-dom";
import { Background } from "./Background";
import { TopBar } from "./TopBar";
import { useSession } from "./session";
import { Login } from "./pages/Login";
import { Home } from "./pages/Home";
import { Butler } from "./pages/Butler";
import { Items } from "./pages/Items";
import { DocList } from "./pages/DocList";
import { DocForm } from "./pages/DocForm";
import { Accounts } from "./pages/Accounts";
import { Till } from "./pages/Till";
import { useCallback, useState } from "react";
import { useOutboxSync } from "./pos/sync";

export function App() {
  const { me, ready, t } = useSession();
  const [refused, setRefused] = useState<string | null>(null);
  const onRefused = useCallback((m: string) => setRefused(m), []);
  useOutboxSync(!!me, onRefused);
  if (!ready) {
    return (
      <>
        <Background />
        <div className="center muted">{t("loading")}</div>
      </>
    );
  }
  if (!me) {
    return (
      <>
        <Background />
        <Login />
      </>
    );
  }
  return (
    <>
      <Background />
      <TopBar />
      <main className="page">
        {refused && (
          <div className="card glass error" style={{ minHeight: 0, marginBottom: 14 }}
               onClick={() => setRefused(null)}>{t("offlineFailed")}: {refused}</div>
        )}
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/butler" element={<Butler />} />
          <Route path="/items" element={<Items />} />
          <Route path="/sales" element={<DocList area="sales" />} />
          <Route path="/purchases" element={<DocList area="purchase" />} />
          <Route path="/finance" element={<DocList area="finance" />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/docs/:kind/:id" element={<DocForm />} />
          <Route path="/till" element={<Till />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
