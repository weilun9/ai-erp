import { useCallback, useEffect, useMemo, useState } from "react";
import { api, money } from "../api";
import { useSession } from "../session";
import { errorText, isNum, today } from "../docs/shared";

// Bank reconciliation (tick the bank statement against the books) and year-end close.
// The server keeps the matching; this screen collects the statement and the ticks.

interface Account { id: string; code: string; name: string; type: string; system_key: string | null }
interface StmtLine {
  id: string; txn_date: string; amount: string; description: string | null; reference: string | null;
  matched_line_ids: string[];
}
interface BookLine {
  id: string; date: string; amount: string; journal_no: string; source_type: string;
  source_no: string | null; memo: string | null;
}
interface Rec {
  book_balance: string; deposits_in_transit: string; unpresented_payments: string;
  expected_statement_balance: string; difference: string | null;
}

const cents = (v: string | number) => Math.round(Number(v) * 100);

/** "date  text  amount" rows from a bank's CSV or an Excel copy. Two numbers at the end are
 *  read as withdrawal, deposit. Dates: YYYY-MM-DD or DD/MM/YYYY. */
export function parseStatement(text: string) {
  const out: { txn_date: string; amount: string; description: string }[] = [];
  const bad: number[] = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const parts = (line.includes("\t") ? line.split("\t") : line.split(/[;,](?=(?:[^"]*"[^"]*")*[^"]*$)/))
      .map((x) => x.trim().replace(/^"|"$/g, ""));
    const iso = (d: string) => {
      const m = d.match(/^(\d{4})-(\d{2})-(\d{2})$/) ?? null;
      if (m) return d;
      const e = d.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$/);
      if (!e) return null;
      const y = e[3].length === 2 ? `20${e[3]}` : e[3];
      return `${y}-${e[2].padStart(2, "0")}-${e[1].padStart(2, "0")}`;
    };
    const date = iso(parts[0] ?? "");
    const num = (s: string) => s.replace(/[,\s]/g, "").replace(/^RM/i, "");
    const isAmt = (s: string) => /^-?\d+(\.\d+)?$/.test(num(s));
    if (!date || parts.length < 2) { bad.push(i + 1); return; }
    let amount: number | null = null;
    let textEnd = parts.length - 1;
    const last = parts[parts.length - 1], prev = parts[parts.length - 2] ?? "";
    if (parts.length >= 4 && (isAmt(last) || last === "") && (isAmt(prev) || prev === "") && (last || prev)) {
      amount = (Number(num(last)) || 0) - (Number(num(prev)) || 0);  // withdrawal, deposit
      textEnd = parts.length - 2;
    } else if (isAmt(last)) {
      amount = Number(num(last));
    }
    if (amount === null || !amount) { bad.push(i + 1); return; }
    out.push({ txn_date: date, amount: amount.toFixed(2), description: parts.slice(1, textEnd).join(" ").trim() });
  });
  return { lines: out, bad };
}

export function BankRec({ accounts }: { accounts: Account[] }) {
  const { t, can } = useSession();
  const write = can("acc.journal.create");
  const [banks, setBanks] = useState<Account[]>([]);
  const [bank, setBank] = useState("");
  const [asOf, setAsOf] = useState(today());
  const [closing, setClosing] = useState("");
  const [stmt, setStmt] = useState<StmtLine[]>([]);
  const [book, setBook] = useState<BookLine[]>([]);
  const [rec, setRec] = useState<Rec | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [ticks, setTicks] = useState<Set<string>>(new Set());
  const [hints, setHints] = useState<Set<string>>(new Set());
  const [paste, setPaste] = useState<string | null>(null);
  const [contra, setContra] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Account[]>("/accounting/bank/accounts").then((xs) => { setBanks(xs); setBank((b) => b || xs[0]?.id || ""); })
      .catch((e) => setError(errorText(e)));
  }, []);

  const load = useCallback(() => {
    if (!bank) return;
    const q = { account_id: bank };
    Promise.all([
      api<StmtLine[]>("/accounting/bank/statement", { query: q }),
      api<BookLine[]>("/accounting/bank/book", { query: q }),
      api<Rec>("/accounting/bank/reconciliation", {
        query: { ...q, as_of: asOf, ...(isNum(closing) ? { statement_balance: closing } : {}) },
      }),
    ]).then(([s, b, r]) => { setStmt(s); setBook(b); setRec(r); setError(null); })
      .catch((e) => setError(errorText(e)));
  }, [bank, asOf, closing]);
  useEffect(load, [load]);

  const open = useMemo(() => stmt.filter((x) => !x.matched_line_ids.length), [stmt]);
  const done = useMemo(() => stmt.filter((x) => x.matched_line_ids.length), [stmt]);
  const line = open.find((x) => x.id === picked) ?? null;
  const tickSum = book.filter((b) => ticks.has(b.id)).reduce((s, b) => s + cents(b.amount), 0);
  const fits = !!line && ticks.size > 0 && tickSum === cents(line.amount);

  const pick = (id: string | null) => {
    setPicked(id);
    setTicks(new Set());
    setHints(new Set());
    if (id) {
      api<BookLine[]>(`/accounting/bank/statement/${id}/suggest`).then((xs) => {
        setHints(new Set(xs.map((x) => x.id)));
        if (xs.length === 1) setTicks(new Set([xs[0].id]));
      }).catch(() => undefined);
    }
  };
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true); setError(null); setMsg(null);
    try { await fn(); if (ok) setMsg(ok); pick(null); load(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const parsed = paste !== null ? parseStatement(paste) : null;

  return (
    <>
      <section className="card glass">
        <div className="doc-head">
          <div className="field">
            <label htmlFor="br-bank">{t("bankAccount")}</label>
            <select id="br-bank" className="input" value={bank} onChange={(e) => { setBank(e.target.value); pick(null); }}>
              {banks.map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="br-asof">{t("bankAsOf")}</label>
            <input id="br-asof" type="date" className="input" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="br-close">{t("bankClosing")}</label>
            <input id="br-close" className="input num" inputMode="decimal" value={closing} placeholder="0.00"
                   onChange={(e) => setClosing(e.target.value)} />
          </div>
        </div>
        {rec && (
          <div className="rec-sum">
            <div><span>{t("bankBook")}</span><b>{money(rec.book_balance)}</b></div>
            <div><span>− {t("bankInTransit")}</span><b>{money(rec.deposits_in_transit)}</b></div>
            <div><span>+ {t("bankUnpresented")}</span><b>{money(rec.unpresented_payments)}</b></div>
            <div className="grand"><span>{t("bankExpected")}</span><b>{money(rec.expected_statement_balance)}</b></div>
            {rec.difference !== null && (
              <div className={cents(rec.difference) === 0 ? "rec-ok" : "rec-off"}>
                <span>{t("bankDifference")}</span>
                <b>{cents(rec.difference) === 0 ? `✓ ${t("bankBalanced")}` : money(rec.difference)}</b>
              </div>
            )}
          </div>
        )}
        {write && (
          <div className="row" style={{ justifyContent: "flex-start", flexWrap: "wrap" }}>
            <button className="btn ghost" onClick={() => setPaste(paste === null ? "" : null)}>{t("bankPaste")}</button>
            <button className="btn" disabled={busy || !open.length}
                    onClick={() => run(async () => {
                      const r = await api<{ matched: number }>("/accounting/bank/auto-match", { method: "POST", query: { account_id: bank } });
                      setMsg(t("bankAutoDone").replace("{n}", String(r.matched)));
                    })}>{t("bankAuto")}</button>
          </div>
        )}
        {paste !== null && (
          <div className="paste-box">
            <span className="muted">{t("bankPasteHint")}</span>
            <textarea className="input" rows={6} value={paste} placeholder={"2026-11-03\tCash deposit\t1,000.00\n03/11/2026, Bank charge, -15.00"}
                      onChange={(e) => setPaste(e.target.value)} />
            {parsed && (
              <span className="muted">
                {t("bankParsed").replace("{n}", String(parsed.lines.length))}
                {parsed.bad.length > 0 && ` · ${t("bankBadRows")} ${parsed.bad.join(", ")}`}
              </span>
            )}
            <button className="btn" disabled={busy || !parsed?.lines.length}
                    onClick={() => run(async () => {
                      const r = await api<{ added: number; skipped: number }>("/accounting/bank/statement", {
                        method: "POST", body: { account_id: bank, lines: parsed!.lines },
                      });
                      setPaste(null);
                      setMsg(t("bankAdded").replace("{n}", String(r.added)).replace("{s}", String(r.skipped)));
                    })}>{t("bankAddLines")}</button>
          </div>
        )}
        {msg && <span className="ok-text">{msg}</span>}
        {error && <div className="error" role="alert">{error}</div>}
      </section>

      <div className="rec-pair">
        <section className="card glass">
          <h3 style={{ margin: 0 }}>{t("bankStatementSide")} <span className="muted">({open.length})</span></h3>
          {!open.length ? <span className="muted">{t("bankAllMatched")}</span> : (
            <table className="table one-line">
              <thead><tr><th>{t("date")}</th><th>{t("bankText")}</th><th className="num">{t("amount")}</th></tr></thead>
              <tbody>
                {open.map((x) => (
                  <tr key={x.id} className={`click${picked === x.id ? " picked" : ""}`} onClick={() => pick(picked === x.id ? null : x.id)}>
                    <td>{x.txn_date}</td>
                    <td>{x.description ?? "—"}{x.reference && <div className="muted small">{x.reference}</div>}</td>
                    <td className={`num ${Number(x.amount) < 0 ? "neg" : ""}`}>{money(x.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {line && write && (
            <div className="rec-actions">
              <span className="muted">
                {t("bankTicked")} {money(tickSum / 100)} / {money(line.amount)}
              </span>
              <button className="btn" disabled={busy || !fits}
                      onClick={() => run(() => api(`/accounting/bank/statement/${line.id}/match`, {
                        method: "POST", body: { journal_line_ids: [...ticks] } }), t("bankMatched"))}>
                {t("bankMatch")}
              </button>
              <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                <select className="input sm" value={contra} aria-label={t("bankBookTo")} onChange={(e) => setContra(e.target.value)}>
                  <option value="">{t("bankBookTo")}</option>
                  {accounts.filter((a) => a.id !== bank && !["AR", "AP", "INVENTORY"].includes(a.system_key ?? ""))
                    .map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name}</option>)}
                </select>
                <button className="chip-btn" disabled={busy || !contra}
                        onClick={() => run(() => api(`/accounting/bank/statement/${line.id}/book`, {
                          method: "POST", body: { account_id: contra } }), t("bankBooked"))}>
                  {t("bankBookIt")}
                </button>
                <button className="chip-btn" disabled={busy}
                        onClick={() => run(() => api(`/accounting/bank/statement/${line.id}`, { method: "DELETE" }))}>
                  {t("remove")}
                </button>
              </div>
            </div>
          )}
        </section>

        <section className="card glass">
          <h3 style={{ margin: 0 }}>{t("bankBookSide")} <span className="muted">({book.length})</span></h3>
          {!book.length ? <span className="muted">{t("bankNoBook")}</span> : (
            <table className="table one-line">
              <thead><tr><th />{/* tick */}<th>{t("date")}</th><th>{t("document")}</th><th className="num">{t("amount")}</th></tr></thead>
              <tbody>
                {book.map((b) => (
                  <tr key={b.id} className={hints.has(b.id) ? "picked" : ""}>
                    <td>
                      <input type="checkbox" disabled={!line || !write} checked={ticks.has(b.id)} aria-label={b.journal_no}
                             onChange={(e) => {
                               const n = new Set(ticks);
                               if (e.target.checked) n.add(b.id); else n.delete(b.id);
                               setTicks(n);
                             }} />
                    </td>
                    <td>{b.date}</td>
                    <td>{b.source_no ?? b.journal_no}<div className="muted small">{b.memo}</div></td>
                    <td className={`num ${Number(b.amount) < 0 ? "neg" : ""}`}>{money(b.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      {done.length > 0 && (
        <section className="card glass">
          <h3 style={{ margin: 0 }}>{t("bankDone")} <span className="muted">({done.length})</span></h3>
          <table className="table one-line">
            <tbody>
              {done.map((x) => (
                <tr key={x.id}>
                  <td>{x.txn_date}</td><td>{x.description ?? "—"}</td>
                  <td className="num">{money(x.amount)}</td>
                  <td className="num">
                    {write && (
                      <button className="chip-btn" disabled={busy}
                              onClick={() => run(() => api(`/accounting/bank/statement/${x.id}/unmatch`, { method: "POST" }))}>
                        {t("bankUnmatch")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}

interface Close { id: string; year_end: string; net_profit: string; closed_at: string; reopened_at: string | null }

export function YearEnd() {
  const { t, can } = useSession();
  const [data, setData] = useState<{ closed_through: string | null; closes: Close[] } | null>(null);
  const [yearEnd, setYearEnd] = useState(`${Number(today().slice(0, 4)) - 1}-12-31`);
  const [sure, setSure] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    api<{ closed_through: string | null; closes: Close[] }>("/accounting/year-end").then(setData)
      .catch((e) => setError(errorText(e)));
  }, []);
  useEffect(load, [load]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); setSure(false); load(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const allowed = can("acc.period.close");
  const latest = data?.closes.find((c) => !c.reopened_at);
  return (
    <section className="card glass">
      <span className="muted">{t("yecHint")}</span>
      <div className="stat-row">
        <span>{t("yecClosedThrough")}</span>
        <b>{data?.closed_through ?? t("yecNone")}</b>
      </div>
      {allowed && (
        <div className="doc-head">
          <div className="field">
            <label htmlFor="yec-date">{t("yecYearEnd")}</label>
            <input id="yec-date" type="date" className="input" value={yearEnd}
                   onChange={(e) => { setYearEnd(e.target.value); setSure(false); }} />
          </div>
          <label className="check-row">
            <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} />
            {t("yecSure").replace("{d}", yearEnd)}
          </label>
          <button className="btn" style={{ alignSelf: "end" }} disabled={busy || !sure || !yearEnd}
                  onClick={() => act(() => api("/accounting/year-end", { method: "POST", body: { year_end: yearEnd } }))}>
            {t("yecClose")}
          </button>
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
      {data && data.closes.length > 0 && (
        <table className="table one-line">
          <thead><tr><th>{t("yecYearEnd")}</th><th className="num">{t("yecResult")}</th><th>{t("yecClosedAt")}</th><th /></tr></thead>
          <tbody>
            {data.closes.map((c) => (
              <tr key={c.id} className={c.reopened_at ? "muted" : ""}>
                <td>{c.year_end}</td>
                <td className={`num ${Number(c.net_profit) < 0 ? "neg" : ""}`}>{money(c.net_profit)}</td>
                <td>{new Date(c.closed_at).toLocaleDateString("en-CA")}{c.reopened_at && ` · ${t("yecReopened")}`}</td>
                <td className="num">
                  {allowed && latest?.id === c.id && (
                    <button className="chip-btn" disabled={busy}
                            onClick={() => act(() => api("/accounting/year-end/reopen", { method: "POST" }))}>
                      {t("yecReopen")}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
