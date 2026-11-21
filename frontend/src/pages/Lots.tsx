import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { DOC_ROUTE, errorText } from "../docs/shared";

// Batches & serials: what is in stock per store with its expiry date (earliest first),
// what has expired or expires soon, and the trace of one batch / serial number - which
// documents brought it in and which customers it went to.

interface LotRow {
  lot_id: string; item_id: string; item_code: string; item_name: string; kind: string;
  lot_no: string; expiry_date: string | null; days_left: number | null;
  location_id: string; location_code: string; qty: string;
}
interface Trace {
  lot_id: string; item_code: string; item_name: string; kind: string; lot_no: string;
  expiry_date: string | null;
  moves: { at: string; doc_type: string; doc_id: string; doc_no: string; location_code: string;
           qty: string; is_reversal: boolean }[];
}
type Status = "in_stock" | "expiring" | "expired" | "all";

// documents with an entry screen (till sales and stock documents have none to open)
const ROUTES: Record<string, string> = { ...DOC_ROUTE, DO: "delivery-order" };
const DOC_LINK = (type: string, id: string) => (ROUTES[type] ? `/docs/${ROUTES[type]}/${id}` : null);

function expiryClass(days: number | null) {
  if (days === null) return "";
  if (days < 0) return "lot-expired";
  if (days <= 30) return "lot-soon";
  return "";
}

export function Lots() {
  const { t } = useSession();
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") as Status) ?? "in_stock";
  const [q, setQ] = useState(params.get("q") ?? "");
  const [days, setDays] = useState("30");
  const [rows, setRows] = useState<LotRow[] | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => {
      setRows(null);
      api<Page<LotRow>>("/lots", { query: { status, q: q.trim() || undefined, days: Number(days) || 30, page_size: 500 } })
        .then((p) => { setRows(p.items); setError(null); })
        .catch((e) => { setRows([]); setError(errorText(e)); });
    }, q ? 250 : 0);
    return () => clearTimeout(timer);
  }, [status, q, days]);

  const open = (id: string) =>
    api<Trace>(`/lots/${id}/trace`).then(setTrace).catch((e) => setError(errorText(e)));

  const tabs: { id: Status; label: Parameters<typeof t>[0] }[] = [
    { id: "in_stock", label: "lotInStock" },
    { id: "expiring", label: "lotExpiring" },
    { id: "expired", label: "lotExpired" },
    { id: "all", label: "lotAll" },
  ];
  return (
    <div className="two">
      <section className="card glass">
        <h2 style={{ margin: 0 }}>{t("lotsTitle")}</h2>
        <div className="tabs" style={{ margin: 0 }}>
          {tabs.map((x) => (
            <button key={x.id} className={x.id === status ? "on" : ""}
                    onClick={() => setParams({ status: x.id }, { replace: true })}>{t(x.label)}</button>
          ))}
        </div>
        <div className="row">
          <input className="input" style={{ flex: 1 }} value={q} placeholder={t("lotSearch")}
                 aria-label={t("lotSearch")} onChange={(e) => setQ(e.target.value)} />
          {status === "expiring" && (
            <label className="row" style={{ gap: 6, whiteSpace: "nowrap" }}>
              {t("lotWithin")}
              <input className="input sm num" style={{ width: 70 }} inputMode="numeric" value={days}
                     onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
              {t("lotDays")}
            </label>
          )}
        </div>
        {error && <div className="error" role="alert">{error}</div>}
        {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0 ? (
          <span className="muted">{t("lotNone")}</span>
        ) : (
          <div className="scroll-x">
            <table className="table one-line">
              <thead>
                <tr><th>{t("item")}</th><th>{t("lotNumber")}</th><th>{t("lotExpiry")}</th>
                  <th>{t("store")}</th><th className="num">{t("qtyLabel")}</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.lot_id + r.location_id} className={`click${trace?.lot_id === r.lot_id ? " picked" : ""}`}
                      onClick={() => open(r.lot_id)}>
                    <td>{r.item_name}<div className="muted small">{r.item_code}</div></td>
                    <td>{r.kind === "SERIAL" ? "SN " : ""}{r.lot_no === "-" ? <span className="muted">{t("lotUnnumbered")}</span> : r.lot_no}</td>
                    <td className={expiryClass(r.days_left)}>
                      {r.expiry_date ?? "—"}
                      {r.days_left !== null && (
                        <div className="small">
                          {r.days_left < 0 ? t("lotExpiredAgo").replace("{n}", String(-r.days_left))
                            : t("lotDaysLeft").replace("{n}", String(r.days_left))}
                        </div>
                      )}
                    </td>
                    <td>{r.location_code}</td>
                    <td className="num">{qty(r.qty)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card glass">
        {!trace ? <span className="muted">{t("lotPickHint")}</span> : (
          <>
            <h3 style={{ margin: 0 }}>{trace.kind === "SERIAL" ? "SN " : `${t("lotBatch")} `}{trace.lot_no}</h3>
            <span className="muted">
              {trace.item_name} · {trace.item_code}
              {trace.expiry_date && ` · ${t("lotExp")} ${trace.expiry_date}`}
            </span>
            <div className="scroll-x">
              <table className="table one-line">
                <thead><tr><th>{t("date")}</th><th>{t("document")}</th><th>{t("store")}</th>
                  <th className="num">{t("qtyLabel")}</th></tr></thead>
                <tbody>
                  {trace.moves.map((m, i) => {
                    const link = DOC_LINK(m.doc_type, m.doc_id);
                    return (
                      <tr key={i} className={m.is_reversal ? "muted" : ""}>
                        <td>{new Date(m.at).toLocaleDateString("en-CA")}</td>
                        <td>{link ? <Link to={link}>{m.doc_no}</Link> : m.doc_no}
                          {m.is_reversal && <span className="muted"> ({t("lotVoided")})</span>}</td>
                        <td>{m.location_code}</td>
                        <td className={`num ${Number(m.qty) < 0 ? "neg" : ""}`}>
                          {Number(m.qty) > 0 ? "+" : ""}{qty(m.qty)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
