import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useSession } from "./session";
import { useWaiting } from "./pos/sync";

export function TopBar() {
  const { me, t, can, logout, offline } = useSession();
  const waiting = useWaiting();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const initials = (me?.display_name ?? "?").slice(0, 1).toUpperCase();
  const { pathname } = useLocation();
  const tabbar = useRef<HTMLElement>(null);
  // phones: the tab bar scrolls sideways; keep the page you are on visible in it
  useEffect(() => {
    const on = tabbar.current?.querySelector<HTMLElement>("a.active");
    if (on) void on.scrollIntoView({ inline: "center", block: "nearest" });
  }, [pathname]);

  const links = (
    <>
      <NavLink to="/" end>{t("home")}</NavLink>
      {can("pos.sale.create") && <NavLink to="/till">{t("till")}</NavLink>}
      {can("sal.doc.read") && (
        <NavLink to="/sales" className={pathname.startsWith("/parties/customer") ? "active" : undefined}>
          {t("sales")}
        </NavLink>
      )}
      {can("pur.doc.read") && (
        <NavLink to="/purchases" className={pathname.startsWith("/parties/supplier") ? "active" : undefined}>
          {t("purchases")}
        </NavLink>
      )}
      {can("fin.doc.read") && (
        <NavLink to="/finance" className={/^\/(accounts|issues)/.test(pathname) ? "active" : undefined}>
          {t("finance")}
        </NavLink>
      )}
      {can("md.item.read") && <NavLink to="/items">{t("items")}</NavLink>}
      {can("ai.chat.use") && <NavLink to="/butler">{t("butler")}</NavLink>}
    </>
  );

  return (
    <>
    <header className="topbar">
      <div className="brand">
        <Link to="/" className="logo" aria-label={t("appName")}>AI</Link>
        <span className="brand-name">{t("appName")}</span>
        <nav className="nav">{links}</nav>
      </div>
      <form
        className="search glass pill"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          if (q.trim()) nav(`/items?q=${encodeURIComponent(q.trim())}`);
        }}
      >
        <span aria-hidden>⌕</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("search")}
               aria-label={t("search")} />
      </form>
      <div className="user glass pill">
        {offline && <span className="badge red" title={t("offlineHint")}>{t("offline")}</span>}
        {waiting > 0 && <span className="badge amber">{waiting} {t("waitingUpload")}</span>}
        <span>{me?.display_name}</span>
        <button className="chip-btn" onClick={logout}>{t("logout")}</button>
        <div className="avatar" aria-hidden>{initials}</div>
      </div>
    </header>
    <nav className="tabbar" ref={tabbar}>{links}</nav>
    </>
  );
}
