import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, money, qty as fmtQty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { isOffline, outbox } from "../pos/outbox";
import { Receipt } from "../pos/Receipt";
import type { ReceiptData } from "../pos/Receipt";

// --- types --------------------------------------------------------------------------------

interface Shift { id: string; shift_no: string; location_id: string; opening_float: string }
interface Line {
  key: string;
  item_id: string;
  uom: string;
  barcode?: string;
  qty: number;
  unit_price?: string;
  discount_pct?: string;
  name: string;
  code: string;
}
interface QuoteLine {
  item_id: string; code: string; name: string; uom: string; qty: string; unit_price: string;
  list_price: string | null; discount_pct: string; tax_rate: string; line_subtotal: string;
  line_tax: string; line_total: string;
}
interface Quote {
  lines: QuoteLine[]; subtotal: string; tax_total: string; total: string; cash_total: string;
  needs_supervisor: string[];
}
interface Sale {
  kind: "SALE" | "RETURN"; receipt_no: string; sold_at: string; subtotal: string;
  tax_total: string; rounding: string; total: string; tendered: string; change: string;
  payments: { method: string; amount: string }[];
  lines: { line_no: number; item_id: string; item_name?: string; uom: string; qty: string;
           unit_price: string; line_total: string }[];
}
type Method = "CASH" | "CARD" | "EWALLET";

// --- money maths for the offline estimate (the server's figures win when online) ----------

const r2 = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;
const r05 = (v: number) => Math.round((v + Number.EPSILON) * 20) / 20;
const PRICE_KEY = "pos-prices";

type PriceBook = Record<string, { code: string; name: string; price: string; rate: string }>;
function priceBook(): PriceBook {
  try { return JSON.parse(localStorage.getItem(PRICE_KEY) ?? "{}"); } catch { return {}; }
}
function remember(q: Quote) {
  const book = priceBook();
  for (const l of q.lines) {
    if (l.list_price !== null) {
      book[`${l.item_id}|${l.uom}`] = { code: l.code, name: l.name, price: l.list_price,
                                        rate: l.tax_rate };
    }
  }
  try { localStorage.setItem(PRICE_KEY, JSON.stringify(book)); } catch { /* fine */ }
}
const BARCODE_KEY = "pos-barcodes";
type Scanned = { id: string; code: string; name: string; uom: string };
function barcodes(): Record<string, Scanned> {
  try { return JSON.parse(localStorage.getItem(BARCODE_KEY) ?? "{}"); } catch { return {}; }
}
function rememberBarcode(text: string, hit: Scanned) {
  try {
    localStorage.setItem(BARCODE_KEY, JSON.stringify({ ...barcodes(), [text]: hit }));
  } catch { /* fine */ }
}

function estimate(lines: Line[]): Quote | null {
  const book = priceBook();
  const out: QuoteLine[] = [];
  for (const l of lines) {
    const p = book[`${l.item_id}|${l.uom}`];
    if (!p) return null;
    const price = Number(l.unit_price ?? p.price);
    const sub = r2(l.qty * price * (100 - Number(l.discount_pct ?? 0)) / 100);
    const tax = r2(sub * Number(p.rate) / 100);
    out.push({ item_id: l.item_id, code: p.code, name: p.name, uom: l.uom, qty: String(l.qty),
      unit_price: String(price), list_price: p.price, discount_pct: l.discount_pct ?? "0",
      tax_rate: p.rate, line_subtotal: String(sub), line_tax: String(tax),
      line_total: String(r2(sub + tax)) });
  }
  const sub = r2(out.reduce((a, l) => a + Number(l.line_subtotal), 0));
  const tax = r2(out.reduce((a, l) => a + Number(l.line_tax), 0));
  return { lines: out, subtotal: String(sub), tax_total: String(tax), total: String(r2(sub + tax)),
           cash_total: String(r05(sub + tax)), needs_supervisor: [] };
}

// --- small dialogs ------------------------------------------------------------------------

function Modal({ children, onClose }: { children: React.ReactNode; onClose?: () => void }) {
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal glass" onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>
  );
}

function SupervisorDialog({ reasons, onOk, onCancel }: {
  reasons: string[]; onOk: (u: string, p: string) => void; onCancel: () => void;
}) {
  const { t } = useSession();
  const [u, setU] = useState("");
  const [pw, setPw] = useState("");
  return (
    <Modal onClose={onCancel}>
      <h3 style={{ margin: 0 }}>{t("supervisorTitle")}</h3>
      <ul className="list muted">{reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <form className="field" onSubmit={(e) => { e.preventDefault(); onOk(u, pw); }}>
        <input className="input" placeholder={t("username")} value={u} autoFocus
               autoComplete="off" onChange={(e) => setU(e.target.value)} />
        <input className="input" type="password" placeholder={t("password")} value={pw}
               autoComplete="off" onChange={(e) => setPw(e.target.value)} />
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn ghost" onClick={onCancel}>{t("cancel")}</button>
          <button className="btn green">{t("approve")}</button>
        </div>
      </form>
    </Modal>
  );
}

// --- the till -----------------------------------------------------------------------------

export function Till() {
  const { me, t, can } = useSession();
  const [shift, setShift] = useState<Shift | null | undefined>(undefined);
  const [lines, setLines] = useState<Line[]>([]);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [offline, setOffline] = useState(false);
  const [scan, setScan] = useState("");
  const [picks, setPicks] = useState<{ id: string; code: string; name: string; base_uom: string }[]>([]);
  const [method, setMethod] = useState<Method>("CASH");
  const [tendered, setTendered] = useState("");
  const [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [supervisor, setSupervisor] = useState<{ reasons: string[]; retry: (s: object) => void } | null>(null);
  const [receipt, setReceipt] = useState<ReceiptData | null>(null);
  const [panel, setPanel] = useState<"close" | "return" | null>(null);
  const [editing, setEditing] = useState<{ key: string; price: string; disc: string } | null>(null);
  const [waiting, setWaiting] = useState(outbox.list().length);
  const scanRef = useRef<HTMLInputElement>(null);
  const saleId = useRef<string>(crypto.randomUUID()); // one per basket: retries reuse it

  const loadShift = useCallback(() => {
    api<Shift | null>("/pos/shifts/current").then(setShift).catch(() => setShift(null));
  }, []);
  useEffect(() => { loadShift(); }, [loadShift]);
  // The scanner types into the scan box, but never steal focus from an open dialog.
  useEffect(() => {
    if (!receipt && !panel && !supervisor) scanRef.current?.focus();
  }, [shift, receipt, panel, supervisor]);

  // Send waiting offline sales every 15 s.
  useEffect(() => {
    const tick = async () => {
      if (!outbox.list().length) return;
      const r = await outbox.flush();
      setWaiting(outbox.list().length);
      if (r.sent > 0) setOffline(false);  // the server answered: we are back
      if (r.failed.length) setError(`${t("offlineFailed")}: ${r.failed[0].message}`);
    };
    const id = window.setInterval(tick, 15000);
    return () => window.clearInterval(id);
  }, [t]);

  // Price the basket on the server whenever it changes.
  useEffect(() => {
    if (!lines.length) { setQuote(null); return; }
    let live = true;
    const h = window.setTimeout(() => {
      api<Quote>("/pos/quote", { method: "POST", body: { lines: lines.map(wire) } })
        .then((q) => { if (live) { remember(q); setQuote(q); setOffline(false); setError(null); } })
        .catch((e) => {
          if (!live) return;
          if (isOffline(e)) { setOffline(true); setQuote(estimate(lines)); }
          else {
            setQuote(null);  // never charge on figures from an earlier basket
            setError(e instanceof Error ? e.message : String(e));
          }
        });
    }, 200);
    return () => { live = false; window.clearTimeout(h); };
  }, [lines]);

  function wire(l: Line) {
    return {
      ...(l.barcode ? { barcode: l.barcode } : { item_id: l.item_id }),
      uom: l.uom, qty: String(l.qty),
      ...(l.unit_price !== undefined ? { unit_price: l.unit_price } : {}),
      ...(l.discount_pct ? { discount_pct: l.discount_pct } : {}),
    };
  }

  function add(item: { id: string; code: string; name: string }, uom: string, barcode?: string) {
    setLines((ls) => {
      const same = ls.findIndex((l) => l.item_id === item.id && l.uom === uom && !l.unit_price);
      if (same >= 0) return ls.map((l, i) => (i === same ? { ...l, qty: l.qty + 1 } : l));
      return [...ls, { key: crypto.randomUUID(), item_id: item.id, uom, barcode, qty: 1,
                       name: item.name, code: item.code }];
    });
    setScan("");
    setPicks([]);
  }

  async function onScan(e: React.FormEvent) {
    e.preventDefault();
    const text = scan.trim();
    if (!text) return;
    setError(null);
    try {
      if (/^\d{8,14}$/.test(text)) {
        const r = await api<{ hits: { matched_uom: string; item: { id: string; code: string; name: string } }[] }>(
          "/items/lookup", { query: { barcode: text } });
        if (r.hits.length === 1) {
          const h = r.hits[0];
          rememberBarcode(text, { ...h.item, uom: h.matched_uom });
          return add(h.item, h.matched_uom, text);
        }
        if (r.hits.length === 0) return setError(`${t("notFound")}: ${text}`);
      }
      const p = await api<Page<{ id: string; code: string; name: string; base_uom: string }>>(
        "/items", { query: { q: text, page_size: 8 } });
      if (p.items.length === 1) add(p.items[0], p.items[0].base_uom);
      else if (p.items.length) setPicks(p.items);
      else setError(`${t("notFound")}: ${text}`);
    } catch (err) {
      const known = barcodes()[text];
      if (isOffline(err) && known) return add(known, known.uom, text);  // scanned before
      setError(isOffline(err) ? t("offlineScan") : err instanceof Error ? err.message : String(err));
    }
  }

  const total = quote ? (method === "CASH" ? quote.cash_total : quote.total) : "0";
  const change = method === "CASH" && tendered ? Math.max(0, Number(tendered) - Number(total)) : 0;

  async function pay(sup?: object) {
    if (!quote || !shift) return;
    const body = {
      client_uuid: saleId.current,
      sold_at: new Date().toISOString(),
      lines: lines.map(wire),
      payments: [{ method, amount: method === "CASH" ? (tendered || total) : quote.total,
                   ...(reference ? { reference } : {}) }],
      ...(sup ? { supervisor: sup } : {}),
    };
    setBusy(true);
    setError(null);
    try {
      const s = await api<Sale>("/pos/sales", { method: "POST", body });
      finish(s, false);
    } catch (e) {
      if (e instanceof ApiError && e.details.code === "SUPERVISOR_REQUIRED") {
        setSupervisor({ reasons: (e.details.reasons as string[]) ?? quote.needs_supervisor,
                        retry: (s) => pay(s) });
      } else if (isOffline(e) && !sup) {
        outbox.push({ path: "/pos/sales", body });
        setWaiting(outbox.list().length);
        const q = quote;
        finish({ kind: "SALE", receipt_no: t("pendingNo"), sold_at: body.sold_at,
          subtotal: q.subtotal, tax_total: q.tax_total,
          rounding: String(r2(Number(total) - Number(q.total))), total,
          tendered: String(body.payments[0].amount), change: String(r2(change)),
          payments: [{ method, amount: total }], lines: [] }, true);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  function finish(s: Sale, wasOffline: boolean) {
    const names = new Map((quote?.lines ?? []).map((l) => [`${l.item_id}|${l.uom}`, l.name]));
    setReceipt({
      kind: s.kind, receipt_no: s.receipt_no, sold_at: s.sold_at, subtotal: s.subtotal,
      tax_total: s.tax_total, rounding: s.rounding, total: s.total, tendered: s.tendered,
      change: s.change, payments: s.payments, offline: wasOffline,
      cashier: me?.display_name ?? "",
      lines: (s.lines.length ? s.lines : (quote?.lines ?? [])).map((l) => ({
        name: ("item_name" in l && l.item_name) || names.get(`${l.item_id}|${l.uom}`) || "",
        uom: l.uom, qty: l.qty,
        unit_price: l.unit_price, line_total: l.line_total,
      })),
    });
    setLines([]);
    setTendered("");
    setReference("");
    saleId.current = crypto.randomUUID();
  }

  if (!can("pos.sale.create")) return <div className="card glass">{t("noAccess")}</div>;
  if (shift === undefined) return <div className="muted">{t("loading")}</div>;
  if (shift === null) return <OpenShift onOpened={setShift} />;

  return (
    <div className="till">
      <section className="glass basket">
        <div className="row" style={{ padding: "14px 16px 0" }}>
          <strong>{shift.shift_no}</strong>
          <div className="row">
            {waiting > 0 && <span className="badge amber">{waiting} {t("waitingUpload")}</span>}
            {offline && <span className="badge red">{t("offline")}</span>}
            {can("pos.return.create") &&
              <button className="btn ghost" onClick={() => setPanel("return")}>{t("refund")}</button>}
            <button className="btn ghost" onClick={() => setPanel("close")}>{t("closeShift")}</button>
          </div>
        </div>
        <form onSubmit={onScan} className="scan">
          <span aria-hidden>▤</span>
          <input ref={scanRef} value={scan} onChange={(e) => setScan(e.target.value)}
                 placeholder={t("scanHere")} aria-label={t("scanHere")} />
        </form>
        {picks.length > 0 && (
          <ul className="picks">
            {picks.map((p) => (
              <li key={p.id}><button onClick={() => add(p, p.base_uom)}>{p.code} · {p.name}</button></li>
            ))}
          </ul>
        )}
        <div className="lines">
          {lines.length === 0 && <div className="empty muted">{t("emptyBasket")}</div>}
          {lines.map((l, i) => {
            const q = quote?.lines[i];
            return (
              <div key={l.key} className="line">
                <div>
                  <div><strong>{l.name}</strong></div>
                  <div className="muted">{l.code} · {l.uom} · {q ? money(q.unit_price) : "…"}
                    {q && Number(q.discount_pct) > 0 && ` −${Number(q.discount_pct)}%`}
                    {q && Number(q.tax_rate) > 0 && ` · SST ${Number(q.tax_rate)}%`}</div>
                </div>
                <div className="qty">
                  <button onClick={() => setLines((ls) => ls.map((x) => x.key === l.key
                    ? { ...x, qty: Math.max(1, x.qty - 1) } : x))}>−</button>
                  <span>{fmtQty(l.qty)}</span>
                  <button onClick={() => setLines((ls) => ls.map((x) => x.key === l.key
                    ? { ...x, qty: x.qty + 1 } : x))}>+</button>
                </div>
                <div className="num"><strong>{q ? money(q.line_total) : "…"}</strong></div>
                <div className="line-tools">
                  <button title={t("changePrice")} onClick={() => setEditing(
                    editing?.key === l.key ? null
                      : { key: l.key, price: l.unit_price ?? q?.unit_price ?? "",
                          disc: l.discount_pct ?? "0" })}>✎</button>
                  <button title={t("remove")}
                          onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}>✕</button>
                </div>
                {editing?.key === l.key && (
                  <form className="line-edit" onSubmit={(e) => {
                    e.preventDefault();
                    const price = editing.price.trim();
                    const disc = editing.disc.trim();
                    if ((price && isNaN(Number(price))) || isNaN(Number(disc || "0"))) return;
                    setLines((ls) => ls.map((x) => x.key === l.key ? {
                      ...x,
                      unit_price: price && price !== q?.list_price ? price : undefined,
                      discount_pct: disc && Number(disc) > 0 ? disc : undefined,
                    } : x));
                    setEditing(null);
                  }}>
                    <label>{t("changePrice")}
                      <input className="input" inputMode="decimal" value={editing.price} autoFocus
                             onChange={(e) => setEditing({ ...editing, price: e.target.value })} />
                    </label>
                    <label>{t("discount")} %
                      <input className="input" inputMode="decimal" value={editing.disc}
                             onChange={(e) => setEditing({ ...editing, disc: e.target.value })} />
                    </label>
                    <button className="btn">OK</button>
                  </form>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <aside className="glass pay">
        <div className="row"><span className="muted">{t("subtotalLabel")}</span><span>{money(quote?.subtotal ?? 0)}</span></div>
        <div className="row"><span className="muted">SST</span><span>{money(quote?.tax_total ?? 0)}</span></div>
        {method === "CASH" && quote && quote.cash_total !== quote.total && (
          <div className="row"><span className="muted">{t("rounding")}</span>
            <span>{money(r2(Number(quote.cash_total) - Number(quote.total)))}</span></div>
        )}
        <div className="row grand"><span>{t("total")}</span><span>{money(total)}</span></div>
        {quote && quote.needs_supervisor.length > 0 && (
          <div className="checks">{t("needsSupervisor")}</div>
        )}
        <div className="tabs methods">
          {(["CASH", "CARD", "EWALLET"] as Method[]).map((m) => (
            <button key={m} className={m === method ? "on" : ""} onClick={() => setMethod(m)}>
              {t(m === "CASH" ? "cash" : m === "CARD" ? "card" : "ewallet")}
            </button>
          ))}
        </div>
        {method === "CASH" ? (
          <>
            <input className="input big-input" inputMode="decimal" placeholder={t("tendered")}
                   value={tendered} onChange={(e) => setTendered(e.target.value)} />
            <div className="quick">
              {quote && [Number(quote.cash_total), 5, 10, 20, 50, 100]
                .map((v, i) => (i === 0 ? v : Math.ceil(Number(quote.cash_total) / v) * v))
                .filter((v, i, a) => v > 0 && a.indexOf(v) === i).slice(0, 4)
                .map((v) => <button key={v} onClick={() => setTendered(v.toFixed(2))}>{money(v)}</button>)}
            </div>
            <div className="row"><span className="muted">{t("change")}</span>
              <strong className="big">{money(change)}</strong></div>
          </>
        ) : (
          <input className="input" placeholder={t("reference")} value={reference}
                 onChange={(e) => setReference(e.target.value)} />
        )}
        {error && <div className="error">{error}</div>}
        <button className="btn green pay-btn" disabled={busy || !quote || (method === "CASH" &&
          !!tendered && Number(tendered) < Number(total))} onClick={() => pay()}>
          {busy ? t("thinking") : `${t("charge")} ${money(total)}`}
        </button>
      </aside>

      {receipt && (
        <Modal onClose={() => setReceipt(null)}>
          <div className="receipt-print"><Receipt r={receipt} shop={t("appName")} /></div>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => window.print()}>{t("print")}</button>
            <button className="btn" autoFocus onClick={() => setReceipt(null)}>{t("nextSale")}</button>
          </div>
        </Modal>
      )}
      {panel === "close" && (
        <CloseShift shift={shift} onDone={() => { setPanel(null); loadShift(); }}
                    onCancel={() => setPanel(null)} />
      )}
      {panel === "return" && (
        <Refund onCancel={() => setPanel(null)} needSupervisor={(reasons, retry) =>
                  setSupervisor({ reasons, retry })}
                onDone={(s) => { setPanel(null); finish(s, false); }} />
      )}
      {/* last, so it sits above the refund dialog that may have asked for it */}
      {supervisor && (
        <SupervisorDialog reasons={supervisor.reasons} onCancel={() => setSupervisor(null)}
          onOk={(username, password) => { const r = supervisor.retry; setSupervisor(null);
                                           r({ username, password }); }} />
      )}
    </div>
  );
}

function OpenShift({ onOpened }: { onOpened: (s: Shift) => void }) {
  const { me, t } = useSession();
  const [stores, setStores] = useState<{ id: string; code: string; name: string }[]>([]);
  const [loc, setLoc] = useState("");
  const [float, setFloat] = useState("200.00");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Page<{ id: string; code: string; name: string }>>("/locations").then((p) => {
      setStores(p.items);
      setLoc(p.items[0]?.id ?? "");
    });
  }, [me]);
  return (
    <div className="center" style={{ minHeight: "60vh" }}>
      <form className="login glass" onSubmit={async (e) => {
        e.preventDefault();
        try {
          onOpened(await api<Shift>("/pos/shifts", { method: "POST",
            body: { location_id: loc, opening_float: float } }));
        } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
      }}>
        <h2 style={{ margin: 0 }}>{t("openShift")}</h2>
        <div className="field"><label>{t("store")}</label>
          <select className="input" value={loc} onChange={(e) => setLoc(e.target.value)}>
            {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
          </select></div>
        <div className="field"><label>{t("float")}</label>
          <input className="input" inputMode="decimal" value={float}
                 onChange={(e) => setFloat(e.target.value)} /></div>
        {error && <div className="error">{error}</div>}
        <button className="btn green">{t("openShift")}</button>
      </form>
    </div>
  );
}

function CloseShift({ shift, onDone, onCancel }: { shift: Shift; onDone: () => void; onCancel: () => void }) {
  const { t } = useSession();
  const [rep, setRep] = useState<{ expected_cash: string; sales: { count: number; total: string };
    returns: { count: number; total: string }; payments: Record<string, string> } | null>(null);
  const [counted, setCounted] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api<typeof rep>(`/pos/shifts/${shift.id}/report`).then(setRep); }, [shift.id]);
  return (
    <Modal onClose={onCancel}>
      <h3 style={{ margin: 0 }}>{t("closeShift")} · {shift.shift_no}</h3>
      {rep && (
        <ul className="list">
          <li><span>{t("salesCount")}</span><span>{rep.sales.count} · {money(rep.sales.total)}</span></li>
          <li><span>{t("refunds")}</span><span>{rep.returns.count} · {money(rep.returns.total)}</span></li>
          {Object.entries(rep.payments).map(([m, v]) => <li key={m}><span>{m}</span><span>{money(v)}</span></li>)}
        </ul>
      )}
      <div className="field"><label>{t("countedCash")}</label>
        <input className="input big-input" inputMode="decimal" value={counted} autoFocus
               onChange={(e) => setCounted(e.target.value)} /></div>
      <div className="field"><label>{t("note")}</label>
        <input className="input" value={note} onChange={(e) => setNote(e.target.value)} /></div>
      {error && <div className="error">{error}</div>}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn ghost" onClick={onCancel}>{t("cancel")}</button>
        <button className="btn" disabled={!counted} onClick={async () => {
          try {
            await api(`/pos/shifts/${shift.id}/close`, { method: "POST",
              body: { counted_cash: counted, note: note || null } });
            onDone();
          } catch (e) {
            const v = e instanceof ApiError ? e.details.variance : undefined;
            setError(v !== undefined ? `${t("varianceNote")} (${money(String(v))})`
              : e instanceof Error ? e.message : String(e));
          }
        }}>{t("closeShift")}</button>
      </div>
    </Modal>
  );
}

function Refund({ onDone, onCancel, needSupervisor }: {
  onDone: (s: Sale) => void; onCancel: () => void;
  needSupervisor: (reasons: string[], retry: (s: object) => void) => void;
}) {
  const { t } = useSession();
  const [no, setNo] = useState("");
  const [sale, setSale] = useState<Sale | null>(null);
  const [back, setBack] = useState<Record<number, string>>({});
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const id = useRef(crypto.randomUUID());

  async function submit(sup?: object) {
    if (!sale) return;
    const lines = Object.entries(back).filter(([, q]) => Number(q) > 0)
      .map(([n, q]) => ({ line_no: Number(n), qty: q }));
    try {
      onDone(await api<Sale>("/pos/returns", { method: "POST", body: {
        client_uuid: id.current, receipt_no: sale.receipt_no, lines, reason: reason || null,
        ...(sup ? { supervisor: sup } : {}) } }));
    } catch (e) {
      if (e instanceof ApiError && e.details.code === "SUPERVISOR_REQUIRED") {
        needSupervisor((e.details.reasons as string[]) ?? [], (s) => submit(s));
      } else setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Modal onClose={onCancel}>
      <h3 style={{ margin: 0 }}>{t("refund")}</h3>
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        setError(null);
        try { setSale(await api<Sale>(`/pos/sales/${encodeURIComponent(no.trim())}`)); }
        catch (err) { setError(err instanceof Error ? err.message : String(err)); }
      }}>
        <input className="input" style={{ flex: 1 }} placeholder="POS000123" value={no} autoFocus
               onChange={(e) => setNo(e.target.value.toUpperCase())} />
        <button className="btn ghost">{t("find")}</button>
      </form>
      {sale && (
        <>
          <ul className="list">
            {sale.lines.map((l) => (
              <li key={l.line_no}>
                <span>{l.item_name} · {fmtQty(l.qty)} {l.uom} · {money(l.line_total)}</span>
                <input className="input" style={{ width: 80 }} inputMode="decimal" placeholder="0"
                       value={back[l.line_no] ?? ""}
                       onChange={(e) => setBack({ ...back, [l.line_no]: e.target.value })} />
              </li>
            ))}
          </ul>
          <input className="input" placeholder={t("reason")} value={reason}
                 onChange={(e) => setReason(e.target.value)} />
        </>
      )}
      {error && <div className="error">{error}</div>}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn ghost" onClick={onCancel}>{t("cancel")}</button>
        <button className="btn" disabled={!sale || !Object.values(back).some((q) => Number(q) > 0)}
                onClick={() => submit()}>{t("refund")}</button>
      </div>
    </Modal>
  );
}
