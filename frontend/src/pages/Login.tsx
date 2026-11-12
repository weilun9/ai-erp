import { useState } from "react";
import { ApiError, login } from "../api";
import { useSession } from "../session";

export function Login() {
  const { t, reload } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [device, setDevice] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
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
      <form className="login glass" onSubmit={submit}>
        <div className="row">
          <div className="brand">
            <div className="logo">AI</div>
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
