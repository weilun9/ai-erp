import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, logout as apiLogout, refresh } from "./api";
import type { Me } from "./api";
import { translator } from "./i18n";
import type { Lang, T } from "./i18n";

interface Session {
  me: Me | null;
  ready: boolean;
  lang: Lang;
  t: T;
  setLang: (l: Lang) => void;
  can: (perm: string) => boolean;
  reload: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<Session | null>(null);

function savedLang(): Lang | null {
  try {
    const v = localStorage.getItem("lang");
    return v === "zh" || v === "en" ? v : null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [lang, setLangState] = useState<Lang>(savedLang() ?? "zh");

  const reload = useCallback(async () => {
    try {
      const m = await api<Me>("/me");
      setMe(m);
      if (!savedLang() && (m.language === "en" || m.language === "zh")) setLangState(m.language);
    } catch {
      setMe(null);
    }
  }, []);

  useEffect(() => {
    // A remembered computer turns its session cookie into a token: no login screen.
    refresh()
      .then((ok) => (ok ? reload() : undefined))
      .finally(() => setReady(true));
  }, [reload]);

  const value = useMemo<Session>(
    () => ({
      me,
      ready,
      lang,
      t: translator(lang),
      setLang: (l) => {
        setLangState(l);
        try {
          localStorage.setItem("lang", l);
        } catch {
          /* private mode: fine, just not remembered */
        }
      },
      can: (perm) => !!me?.permissions.includes(perm),
      reload,
      logout: async () => {
        await apiLogout();
        setMe(null);
      },
    }),
    [me, ready, lang, reload],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
