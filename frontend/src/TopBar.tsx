import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useSession } from "./session";

export function TopBar() {
  const { me, t, lang, setLang, can, logout } = useSession();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const initials = (me?.display_name ?? "?").slice(0, 1).toUpperCase();

  const links = (
    <>
      <NavLink to="/" end>{t("home")}</NavLink>
      {can("pos.sale.create") && <NavLink to="/till">{t("till")}</NavLink>}
      {can("ai.chat.use") && <NavLink to="/butler">{t("butler")}</NavLink>}
      {can("md.item.read") && <NavLink to="/items">{t("items")}</NavLink>}
      {can("sal.doc.read") && <NavLink to="/sales">{t("sales")}</NavLink>}
    </>
  );

  return (
    <>
    <header className="topbar">
      <div className="brand">
        <div className="toggle" role="group" aria-label="Language">
          <button className={lang === "zh" ? "on" : ""} onClick={() => setLang("zh")}>中</button>
          <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button>
        </div>
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
        <span>{me?.display_name}</span>
        <button className="chip-btn" onClick={logout}>{t("logout")}</button>
        <div className="avatar" aria-hidden>{initials}</div>
      </div>
    </header>
    <nav className="tabbar">{links}</nav>
    </>
  );
}
