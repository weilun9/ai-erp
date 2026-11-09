import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, money, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";

function iso(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

function useLoad<T>(enabled: boolean, load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    load()
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
  }, [enabled]);
  return { data, error, loaded };
}

function Card({ title, extra, children, className = "" }: {
  title: string; extra?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card glass ${className}`}>
      <h3>
        <span>{title}</span>
        {extra}
      </h3>
      {children}
    </section>
  );
}

function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  const pts = values.map((v, i) => [(i / (values.length - 1)) * 100, 56 - (v / max) * 48]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  return (
    <svg className="spark" viewBox="0 0 100 60" preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#3f8f3a" stopOpacity="0.35" />
          <stop offset="1" stopColor="#3f8f3a" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line} L100 60 L0 60Z`} fill="url(#sg)" />
      <path d={line} fill="none" stroke="#3f8f3a" strokeWidth="2" vectorEffect="non-scaling-stroke"
            strokeLinejoin="round" />
    </svg>
  );
}

interface Summary {
  rows: { day: string; sales: string }[];
  total_sales: string;
  total_gross_profit?: string;
}

export function Home() {
  const { me, t, can, lang, offline } = useSession();
  const nav = useNavigate();
  const [ask, setAsk] = useState("");

  const today = new Date();
  const start = new Date(today);
  start.setDate(today.getDate() - 6); // the last 7 days, so the trend always has a shape
  const days: string[] = [];
  for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) days.push(iso(d));

  const week = useLoad(can("report.run") && can("sal.doc.read"), () =>
    api<Summary>("/reports/sales-summary", {
      query: { date_from: iso(start), date_to: iso(today), group_by: "day" },
    }),
  );
  const low = useLoad(can("report.run") && can("inv.balance.read"), () =>
    api<{ rows: { item: string; location: string; qty: string; negative: boolean }[] }>(
      "/reports/low-stock", { query: { threshold: 5, limit: 5 } }),
  );
  const issues = useLoad(can("core.issue.read"), () =>
    api<{ title: string; doc_no: string | null }[]>("/issues", { query: { status: "OPEN" } }),
  );
  const pending = useLoad(can("sal.doc.read"), () =>
    api<{ doc_type: string; doc_no: string; total: string }[]>("/einvoice/pending"),
  );
  const recent = useLoad(can("sal.doc.read"), () =>
    api<Page<{ id: string; doc_no: string | null; doc_date: string; total: string }>>(
      "/sales/sales-invoice", { query: { page_size: 4 } }),
  );
  const shift = useLoad(can("pos.sale.create"), async () => {
    const s = await api<{ id: string; shift_no: string } | null>("/pos/shifts/current");
    if (!s) return null;
    const r = await api<{ expected_cash: string; sales: { count: number; total: string } }>(
      `/pos/shifts/${s.id}/report`);
    return { ...s, ...r };
  });

  const byDay = new Map((week.data?.rows ?? []).map((r) => [r.day, Number(r.sales)]));
  const hour = today.getHours();
  const greet = hour < 12 ? "☀" : hour < 18 ? "⛅" : "☾";

  return (
    <>
      <div className="greeting">
        <h1>{t("hello")}, {me?.display_name} {greet}</h1>
        <p>{today.toLocaleDateString(lang === "zh" ? "zh-CN" : "en-MY", { weekday: "long", day: "numeric", month: "long" })}</p>
      </div>
      {offline && (
        <section className="card glass" style={{ minHeight: 0, marginBottom: 18 }}>
          <strong>{t("offlineHint")}</strong>
          {can("pos.sale.create") && <Link className="btn green" style={{ alignSelf: "flex-start",
            textDecoration: "none" }} to="/till">{t("till")} →</Link>}
        </section>
      )}
      <div className="grid">
        {can("ai.chat.use") && (
          <section className="card glass new-card"
                   onClick={() => nav("/butler?ask=" + encodeURIComponent(t("askPlaceholder")))}>
            <div className="plus">+</div>
            <strong>{t("quickNew")}</strong>
            <span className="muted">{t("quickNewHint")}</span>
          </section>
        )}

        {can("report.run") && can("sal.doc.read") && (
          <Card title={t("thisWeek")}>
            {week.error ? <span className="error">{week.error}</span> : !week.data ? (
              <span className="muted">{t("loading")}</span>
            ) : (
              <>
                <div className="big">{money(week.data.total_sales)}</div>
                {week.data.total_gross_profit !== undefined && (
                  <span className="muted">{t("grossProfit")} {money(week.data.total_gross_profit)}</span>
                )}
                <Spark values={days.map((d) => byDay.get(d) ?? 0)} />
              </>
            )}
          </Card>
        )}

        {can("report.run") && can("inv.balance.read") && (
          <Card title={t("lowStock")} extra={<span className="muted">≤ 5 {t("atOrBelow")}</span>}>
            {!low.data ? <span className="muted">{low.error ?? t("loading")}</span>
              : low.data.rows.length === 0 ? <span className="badge green">{t("allGood")}</span>
              : (
                <ul className="list">
                  {low.data.rows.map((r) => (
                    <li key={r.item + r.location}>
                      <span>{r.item} · {r.location}</span>
                      <span className={`badge ${r.negative ? "red" : "amber"}`}>{qty(r.qty)}</span>
                    </li>
                  ))}
                </ul>
              )}
          </Card>
        )}

        {can("sal.doc.read") && (
          <Card title={t("einvoice")}
                extra={pending.data && pending.data.length > 0 &&
                  <span className="badge amber">{pending.data.length}</span>}>
            {!pending.data ? <span className="muted">{pending.error ?? t("loading")}</span>
              : pending.data.length === 0 ? <span className="badge green">{t("nothingToSend")}</span>
              : (
                <ul className="list">
                  {pending.data.slice(0, 4).map((d) => (
                    <li key={d.doc_no}><span>{d.doc_no}</span><span>{money(d.total)}</span></li>
                  ))}
                </ul>
              )}
          </Card>
        )}

        {can("ai.chat.use") && (
          <section className="card glass span-2">
            <h3><span>✦ {t("askButler")}</span></h3>
            <form className="row" onSubmit={(e) => {
              e.preventDefault();
              if (ask.trim()) nav("/butler?ask=" + encodeURIComponent(ask.trim()));
            }}>
              <input className="input" style={{ flex: 1 }} value={ask}
                     placeholder={t("askPlaceholder")} onChange={(e) => setAsk(e.target.value)} />
              <button className="round-btn" aria-label={t("send")}>➤</button>
            </form>
            <div className="tools">
              {["HQ 还有多少 Milo？", "Kedai Ah Seng 欠多少？", "顾客没有收据可以退货吗？"].map((s) => (
                <span key={s} style={{ cursor: "pointer" }}
                      onClick={() => nav("/butler?ask=" + encodeURIComponent(s))}>{s}</span>
              ))}
            </div>
          </section>
        )}

        {can("core.issue.read") && (
          <Card title={t("issues")}
                extra={issues.data && issues.data.length > 0 &&
                  <span className="badge red">{issues.data.length}</span>}>
            {!issues.data ? <span className="muted">{issues.error ?? t("loading")}</span>
              : issues.data.length === 0 ? <span className="badge green">{t("noIssues")}</span>
              : (
                <ul className="list">
                  {issues.data.slice(0, 3).map((i, n) => (
                    <li key={n}><span>{i.title}</span></li>
                  ))}
                </ul>
              )}
          </Card>
        )}

        {can("pos.sale.create") && (
          <Card title={t("shift")}>
            {shift.error ? <span className="error">{shift.error}</span>
              : !shift.loaded ? <span className="muted">{t("loading")}</span>
              : shift.data === null ? <span className="muted">{t("noShift")}</span>
              : (
                <>
                  <div className="big">{money(shift.data.sales.total)}</div>
                  <span className="muted">
                    {shift.data.shift_no} · {shift.data.sales.count} ·{" "}
                    {t("expectedCash")} {money(shift.data.expected_cash)}
                  </span>
                </>
              )}
          </Card>
        )}

        {can("sal.doc.read") && (
          <Card title={t("recent")} extra={<Link className="round-btn" to="/sales">→</Link>}>
            {!recent.data ? <span className="muted">{recent.error ?? t("loading")}</span>
              : recent.data.items.length === 0 ? <span className="muted">{t("noResults")}</span>
              : (
                <ul className="list">
                  {recent.data.items.map((d) => (
                    <li key={d.id}>
                      <span>{d.doc_no ?? "—"} <span className="muted">{d.doc_date}</span></span>
                      <span>{money(d.total)}</span>
                    </li>
                  ))}
                </ul>
              )}
          </Card>
        )}
      </div>
    </>
  );
}
