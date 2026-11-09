import { Navigate, Route, Routes } from "react-router-dom";
import { Background } from "./Background";
import { TopBar } from "./TopBar";
import { useSession } from "./session";
import { Login } from "./pages/Login";
import { Home } from "./pages/Home";
import { Butler } from "./pages/Butler";
import { Items } from "./pages/Items";
import { Sales } from "./pages/Sales";

export function App() {
  const { me, ready, t } = useSession();
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
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/butler" element={<Butler />} />
          <Route path="/items" element={<Items />} />
          <Route path="/sales" element={<Sales />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
