import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, logout as apiLogout, refreshSession } from "./api";
import type { Me } from "./api";
import { translator } from "./i18n";
import type { T } from "./i18n";

interface Session {
  me: Me | null;
  ready: boolean;
  /** No connection: the profile is the one saved at the last login on this computer. */
  offline: boolean;
  t: T;
  can: (perm: string) => boolean;
  reload: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<Session | null>(null);
const ME_KEY = "me-cache";
const t = translator();

function cachedMe(): Me | null {
  try {
    return JSON.parse(localStorage.getItem(ME_KEY) ?? "null") as Me | null;
  } catch {
    return null;
  }
}

function saveMe(m: Me | null) {
  try {
    if (m) localStorage.setItem(ME_KEY, JSON.stringify(m));
    else localStorage.removeItem(ME_KEY);
  } catch {
    /* private mode: fine, just no offline start */
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [offline, setOffline] = useState(false);

  const reload = useCallback(async () => {
    try {
      const m = await api<Me>("/me");
      setMe(m);
      saveMe(m);
      setOffline(false);
    } catch {
      setMe(null);
    }
  }, []);

  // Start: a remembered computer turns its session cookie into a token. With no
  // connection, a computer that was logged in keeps working offline (the till), and
  // the server checks everything again when the queued sales are uploaded.
  const start = useCallback(async () => {
    const r = await refreshSession();
    if (r === "ok") await reload();
    else if (r === "offline" && cachedMe()) {
      setMe(cachedMe());
      setOffline(true);
    } else {
      setMe(null);
      if (r === "denied") saveMe(null);
    }
  }, [reload]);

  useEffect(() => {
    start().finally(() => setReady(true));
  }, [start]);

  // Back online: confirm the login with the server again.
  useEffect(() => {
    if (!offline) return;
    const retry = () => { start(); };
    window.addEventListener("online", retry);
    const id = window.setInterval(retry, 20000);
    return () => {
      window.removeEventListener("online", retry);
      window.clearInterval(id);
    };
  }, [offline, start]);

  const value = useMemo<Session>(
    () => ({
      me,
      ready,
      offline,
      t,
      can: (perm) => !!me?.permissions.includes(perm),
      reload,
      logout: async () => {
        await apiLogout().catch(() => undefined);
        saveMe(null);
        setMe(null);
      },
    }),
    [me, ready, offline, reload],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
