import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText, today } from "../docs/shared";
import type { Key } from "../i18n";

// Business intelligence (DynaMod's B.I. dashboard): the headline figures for a period against
// the period just before it, the sales / gross-profit trend, what sells best and to whom, and
// how sales split by channel and payment method. Cost and profit appear only for users who
// may see cost; balances only for those who may see finance.
// Colours: the validated dark categorical slots (blue, orange, aqua), one axis, text in ink.

interface Headline { sales: string; documents: number; avg_sale: string; gross_profit?: string; margin_pct?: string }
interface Ranked { key: string | null; label: string; qty: string; sales: string; gross_profit?: string }
interface Bi {
  date_from: string; date_to: string; bucket: "day" | "week" | "month";
  current: Headline; previous: Headline & { date_from: string; date_to: string };
  trend: { period: string; sales: string; gross_profit?: string }[];
  items: Ranked[]; customers: Ranked[]; categories: Ranked[]; brands: Ranked[]; stores: Ranked[];
  channels: Ranked[]; payments: { method: string; amount: string }[];
  snapshot: { stock_value?: string; customers_owe?: string; we_owe_suppliers?: string };
}

const SERIES = ["#3987e5", "#d95926", "#199e70"];  // categorical slots 1-3, dark mode
const iso = (d: Date) => d.toLocaleDateString("en-CA");
const daysAgo = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };
const PRESETS: { id: string; label: Key; from: () => string }[] = [
  { id: "7", label: "last7", from: () => daysAgo(6) },
  { id: "30", label: "last30", from: () => daysAgo(29) },
  { id: "month", label: "thisMonth", from: () => `${today().slice(0, 7)}-01` },
  { id: "90", label: "last90", from: () => daysAgo(89) },
  { id: "year", label: "thisYear", from: () => `${today().slice(0, 4)}-01-01` },
];
const n = (v: string | number | undefined) => Number(v ?? 0);
const short = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : v.toFixed(0));

export function Insights() {
  const { t, can } = useSession();
  const [preset, setPreset] = useState("30");
  const [from, setFrom] = useState(daysAgo(29));
  const [to, setTo] = useState(today());
  const [store, setStore] = useState("");
  const [stores, setStores] = useState<{ id: string; code: string }[]>([]);
  const [data, setData] = useState<Bi | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Page<{ id: string; code: string }>>("/locations").then((p) => setStores(p.items)).catch(() => undefined);
  }, []);
  useEffect(() => {
    setError(null);
    api<Bi>("/reports/bi", { query: { date_from: from, date_to: to, ...(store ? { location_id: store } : {}) } })
      .then(setData).catch((e) => { setData(null); setError(errorText(e)); });
  }, [from, to, store]);

  if (!(can("report.run") && can("sal.doc.read"))) {
    return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  }
  const pick = (id: string) => {
    const p = PRESETS.find((x) => x.id === id);
    setPreset(id);
    if (p) { setFrom(p.from()); setTo(today()); }
  };
  const cost = data?.current.gross_profit !== undefined;

  return (
    <div className="insights">
      <section className="card glass bi-filters" style={{ minHeight: 0 }}>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <h2 style={{ margin: 0, fontWeight: 500 }}>{t("insights")}</h2>
          <div className="tabs" style={{ margin: 0 }} role="group" aria-label={t("period")}>
            {PRESETS.map((p) => (
              <button key={p.id} className={preset === p.id ? "on" : ""} onClick={() => pick(p.id)}>{t(p.label)}</button>
            ))}
          </div>
        </div>
        <div className="row" style={{ justifyContent: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <label className="field"><span className="muted">{t("dateFrom")}</span>
            <input className="input sm" type="date" value={from} onChange={(e) => { setPreset(""); setFrom(e.target.value); }} /></label>
          <label className="field"><span className="muted">{t("dateTo")}</span>
            <input className="input sm" type="date" value={to} onChange={(e) => { setPreset(""); setTo(e.target.value); }} /></label>
          <label className="field"><span className="muted">{t("store")}</span>
            <select className="input sm" value={store} onChange={(e) => setStore(e.target.value)}>
              <option value="">{t("allStores")}</option>
              {stores.map((s) => <option key={s.id} value={s.id}>{s.code}</option>)}
            </select></label>
          {data && <span className="muted" style={{ alignSelf: "flex-end" }}>
            {t("comparedWith")} {data.previous.date_from} – {data.previous.date_to}</span>}
        </div>
      </section>
      {error && <div className="error" role="alert">{error}</div>}
      {!data ? !error && <span className="muted">{t("loading")}</span> : (
        <>
          <div className="bi-tiles">
            <Tile glow="green" label={t("netSales")} value={money(data.current.sales)}
                  now={n(data.current.sales)} before={n(data.previous.sales)} delay={0.05} />
            {cost && <Tile glow="blue" label={t("grossProfit")} value={money(data.current.gross_profit)}
                           now={n(data.current.gross_profit)} before={n(data.previous.gross_profit)} delay={0.1}
                           hint={`${n(data.current.margin_pct).toFixed(1)}% ${t("margin")}`} />}
            <Tile glow="violet" label={t("salesDocs")} value={String(data.current.documents)}
                  now={data.current.documents} before={data.previous.documents} delay={0.15}
                  hint={`${t("avgSale")} ${money(data.current.avg_sale)}`} />
            {data.snapshot.stock_value !== undefined && (
              <Tile glow="pink" label={t("stockValue")} value={money(data.snapshot.stock_value)} delay={0.2}
                    hint={t("atCostNow")} />
            )}
          </div>
          {data.snapshot.customers_owe !== undefined && (
            <div className="bi-balances muted">
              <span>{t("customersOwe")}: <b>{money(data.snapshot.customers_owe)}</b></span>
              <span>{t("weOweSuppliers")}: <b>{money(data.snapshot.we_owe_suppliers)}</b></span>
            </div>
          )}

          <section className="card glass" style={{ minHeight: 0 }}>
            <Trend data={data} cost={cost} />
          </section>

          <div className="bi-grid">
            <Ranking title={t("topItems")} rows={data.items} cost={cost} unitLabel />
            <Ranking title={t("topCustomers")} rows={data.customers} cost={cost} />
            <Ranking title={t("byCategory")} rows={data.categories} cost={cost} />
            <Ranking title={t("byBrand")} rows={data.brands} cost={cost} />
            {data.stores.length > 1 && <Ranking title={t("byStore")} rows={data.stores} cost={cost} />}
            <section className="card glass" style={{ minHeight: 0 }}>
              <h3><span>{t("salesMix")}</span></h3>
              <Mix label={t("byChannel")} parts={data.channels.map((c) => ({
                label: t(c.label === "POS" ? "channelPos" : "channelInvoice"), value: n(c.sales) }))} />
              <Mix label={t("posPayments")} parts={data.payments.map((p) => ({
                label: t(`method${p.method}` as Key), value: n(p.amount) }))} />
            </section>
          </div>
        </>
      )}
    </div>
  );
}

function Tile({ label, value, now, before, hint, glow, delay }: {
  label: string; value: string; now?: number; before?: number; hint?: string;
  glow: "green" | "blue" | "pink" | "violet"; delay: number;
}) {
  const { t } = useSession();
  let delta: { text: string; up: boolean } | null = null;
  if (now !== undefined && before !== undefined) {
    if (before) {
      const pct = ((now - before) / Math.abs(before)) * 100;
      delta = { text: `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}%`, up: pct >= 0 };
    } else if (now) {
      delta = { text: `▲ ${t("newThisPeriod")}`, up: true };
    }
  }
  return (
    <div className="glow-wrap" style={{ ["--g" as string]: `var(--g-${glow})`, animationDelay: `${delay}s` } as CSSProperties}>
      <div className="halo" aria-hidden="true" />
      <section className="card glass bi-tile">
        <h3><span>{label}</span></h3>
        <div className="big">{value}</div>
        <span className="muted">
          {delta && <b className={delta.up ? "delta up" : "delta down"}>{delta.text}</b>}
          {delta && " "}{delta ? t("vsPrevious") : hint}
        </span>
        {delta && hint && <span className="muted">{hint}</span>}
      </section>
    </div>
  );
}

/** Sales and gross profit over the period: one axis (both are ringgit), crosshair tooltip,
 * a table view for screen readers and exact values. */
function Trend({ data, cost }: { data: Bi; cost: boolean }) {
  const { t } = useSession();
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const W = 1000, H = 260, L = 52, R = 12, T = 12, B = 28;
  const pts = data.trend;
  const max = Math.max(1, ...pts.map((p) => Math.max(n(p.sales), n(p.gross_profit))));
  const min = Math.min(0, ...pts.map((p) => n(p.gross_profit)));
  const ticks = useMemo(() => {
    // ticks run from a round number at or below the lowest point to one at or above the highest
    const step = niceStep((max - min) / 4);
    const lo = Math.floor(min / step), hi = Math.ceil(max / step);
    const out = [];
    for (let k = lo; k <= hi; k++) out.push(k * step);
    return out;
  }, [max, min]);
  const top = ticks[ticks.length - 1], bottom = ticks[0];
  const x = (i: number) => L + (pts.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (pts.length - 1));
  const y = (v: number) => T + ((top - v) * (H - T - B)) / (top - bottom || 1);
  const line = (k: "sales" | "gross_profit") => pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(n(p[k])).toFixed(1)}`).join(" ");
  const fmt = (d: string) => data.bucket === "month" ? d.slice(0, 7) : d.slice(5);
  const every = Math.max(1, Math.ceil(pts.length / 8));
  const onMove = (e: React.MouseEvent) => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - L) / (W - L - R)) * (pts.length - 1));
    setHover(Math.max(0, Math.min(pts.length - 1, i)));
  };
  const h = hover !== null ? pts[hover] : null;

  return (
    <>
      <div className="row">
        <h3 style={{ margin: 0 }}><span>{t(`trendBy_${data.bucket}` as Key)}</span></h3>
        <div className="row" style={{ gap: 14 }}>
          <span className="legend"><i style={{ background: SERIES[0] }} />{t("netSales")}</span>
          {cost && <span className="legend"><i style={{ background: SERIES[1] }} />{t("grossProfit")}</span>}
          <button className="chip-btn" onClick={() => setTable((v) => !v)}>{t(table ? "showChart" : "showTable")}</button>
        </div>
      </div>
      {table ? (
        <div className="scroll-x">
          <table className="table">
            <thead><tr><th>{t("period")}</th><th className="num">{t("netSales")}</th>{cost && <th className="num">{t("grossProfit")}</th>}</tr></thead>
            <tbody>{pts.map((p) => (
              <tr key={p.period}><td>{p.period}</td><td className="num">{money(p.sales)}</td>
                {cost && <td className="num">{money(p.gross_profit)}</td>}</tr>))}</tbody>
          </table>
        </div>
      ) : (
        <div className="bi-chart" ref={box} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
               aria-label={`${t("netSales")} ${money(data.current.sales)}`}>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="rgba(255,255,255,0.07)" strokeWidth="1"
                      vectorEffect="non-scaling-stroke" />
              </g>
            ))}
            <path d={`${line("sales")} L${x(pts.length - 1)} ${y(0)} L${x(0)} ${y(0)}Z`} fill={SERIES[0]} opacity="0.12" />
            <path d={line("sales")} fill="none" stroke={SERIES[0]} strokeWidth="2" vectorEffect="non-scaling-stroke"
                  strokeLinejoin="round" />
            {cost && <path d={line("gross_profit")} fill="none" stroke={SERIES[1]} strokeWidth="2"
                           vectorEffect="non-scaling-stroke" strokeLinejoin="round" />}
            {hover !== null && (
              <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="rgba(255,255,255,0.35)"
                    strokeWidth="1" vectorEffect="non-scaling-stroke" />
            )}
          </svg>
          {/* axis text in HTML so it does not stretch with the chart */}
          {ticks.map((v) => (
            <span key={v} className="axis-y" style={{ top: `${(y(v) / H) * 100}%` }}>{short(v)}</span>
          ))}
          {pts.map((p, i) => i % every === 0 && (
            <span key={p.period} className="axis-x" style={{ left: `${(x(i) / W) * 100}%` }}>{fmt(p.period)}</span>
          ))}
          {h && hover !== null && (
            <>
              <span className="dot" style={{ left: `${(x(hover) / W) * 100}%`, top: `${(y(n(h.sales)) / H) * 100}%`, background: SERIES[0] }} />
              {cost && <span className="dot" style={{ left: `${(x(hover) / W) * 100}%`, top: `${(y(n(h.gross_profit)) / H) * 100}%`, background: SERIES[1] }} />}
              <div className="tip" style={{ left: `${Math.min(80, (x(hover) / W) * 100)}%` }}>
                <b>{h.period}</b>
                <span><i style={{ background: SERIES[0] }} />{t("netSales")} {money(h.sales)}</span>
                {cost && <span><i style={{ background: SERIES[1] }} />{t("grossProfit")} {money(h.gross_profit)}</span>}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}

function niceStep(raw: number) {
  if (raw <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/** Horizontal bars, one series: the bar shows sales; the numbers sit in ink beside it. */
function Ranking({ title, rows, cost, unitLabel }: { title: string; rows: Ranked[]; cost: boolean; unitLabel?: boolean }) {
  const { t } = useSession();
  const max = Math.max(1, ...rows.map((r) => n(r.sales)));
  return (
    <section className="card glass" style={{ minHeight: 0 }}>
      <h3><span>{title}</span></h3>
      {rows.length === 0 ? <span className="muted">{t("noSalesYet")}</span> : (
        <ul className="bars">
          {rows.map((r) => (
            <li key={r.key ?? r.label} title={`${r.label}: ${money(r.sales)}${cost ? ` · ${t("grossProfit")} ${money(r.gross_profit)}` : ""}${unitLabel ? ` · ${Number(r.qty)} ${t("unitsSold")}` : ""}`}>
              <div className="bar-label">
                <span className="bar-name">{r.label}</span>
                <span className="num">{money(r.sales)}{cost && <span className="muted"> · {money(r.gross_profit)}</span>}</span>
              </div>
              <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.max(1, (n(r.sales) / max) * 100)}%` }} /></div>
            </li>
          ))}
        </ul>
      )}
      {cost && rows.length > 0 && <span className="muted">{t("salesThenProfit")}</span>}
    </section>
  );
}

/** A 100% bar split into at most three parts (slots 1-3), with a legend that names each. */
function Mix({ label, parts }: { label: string; parts: { label: string; value: number }[] }) {
  const { t } = useSession();
  const shown = parts.filter((p) => p.value > 0);
  const top = shown.slice(0, 2);
  const rest = shown.slice(2).reduce((s, p) => s + p.value, 0);
  const all = rest ? [...top, { label: t("other"), value: rest }] : top;
  const total = all.reduce((s, p) => s + p.value, 0);
  return (
    <div className="mix">
      <span className="muted">{label}</span>
      {total === 0 ? <span className="muted">{t("noSalesYet")}</span> : (
        <>
          <div className="mix-bar" role="img" aria-label={all.map((p) => `${p.label} ${Math.round((p.value / total) * 100)}%`).join(", ")}>
            {all.map((p, i) => (
              <div key={p.label} style={{ flex: p.value, background: SERIES[i] }} title={`${p.label}: ${money(p.value)}`} />
            ))}
          </div>
          <div className="mix-legend">
            {all.map((p, i) => (
              <span key={p.label} className="legend"><i style={{ background: SERIES[i] }} />
                {p.label} <b>{Math.round((p.value / total) * 100)}%</b> <span className="muted">{money(p.value)}</span></span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
