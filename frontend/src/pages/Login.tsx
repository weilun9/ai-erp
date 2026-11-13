import { useEffect, useState } from "react";
import { ApiError, api, login } from "../api";
import { useSession } from "../session";

// What the server says about itself before anyone is logged in. On the public demo site
// it lists the demo accounts, so a visitor gets in with one click.
interface DemoInfo {
  demo: boolean;
  accounts?: { username: string; label: string }[];
  password?: string;
}

export function Login() {
  const { t, reload } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [device, setDevice] = useState<string | null>(null);
  const [demo, setDemo] = useState<DemoInfo | null>(null);

  useEffect(() => {
    api<DemoInfo>("/public/demo", { timeoutMs: 8000 }).then(setDemo).catch(() => setDemo(null));
  }, []);

  async function enter(user: string, pass: string) {
    setBusy(true);
    setError(null);
    try {
      await login(user, pass);
      setDevice(null);
      await reload();
    } catch (err) {
      if (err instanceof ApiError && err.code === "DEVICE_NOT_AUTHORIZED") {
        setDevice(String(err.details.device_id ?? ""));
      } else if (err instanceof ApiError && err.status === 401) {
        setError(t("badLogin"));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <form className="login glass" onSubmit={(e) => { e.preventDefault(); enter(username, password); }}>
        <div className="row">
          <div className="brand">
            <h1>{t("appName")}</h1>
          </div>
        </div>
        {device ? (
          <>
            <h3 style={{ margin: 0 }}>{t("deviceTitle")}</h3>
            <p className="muted" style={{ margin: 0 }}>{t("deviceBody")}</p>
            <div className="field">
              <label>{t("deviceId")}</label>
              <div className="code">{device}</div>
            </div>
            <button className="btn" disabled={busy}>{busy ? t("signingIn") : t("tryAgain")}</button>
          </>
        ) : (
          <>
            {demo?.demo && demo.accounts && demo.password && (
              <div className="demo-box">
                <strong>{t("demoTitle")}</strong>
                <span className="muted">{t("demoBody")}</span>
                <div className="row" style={{ justifyContent: "flex-start", flexWrap: "wrap", gap: 8 }}>
                  {demo.accounts.map((a) => (
                    <button key={a.username} type="button" className="btn green" disabled={busy}
                            onClick={() => enter(a.username, demo.password as string)}>
                      {t("enterAs")} {a.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="field">
              <label htmlFor="u">{t("username")}</label>
              <input id="u" className="input" value={username} autoComplete="username"
                     onChange={(e) => setUsername(e.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="p">{t("password")}</label>
              <input id="p" className="input" type="password" value={password}
                     autoComplete="current-password"
                     onChange={(e) => setPassword(e.target.value)} required />
            </div>
            {error && <div className="error">{error}</div>}
            <button className="btn" disabled={busy}>{busy ? t("signingIn") : t("login")}</button>
          </>
        )}
      </form>
    </div>
  );
}
