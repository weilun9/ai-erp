import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { errorText, isNum, round2, today } from "../docs/shared";
import type { Key } from "../i18n";

// 往来账: one customer's or supplier's account.
// - open items: every posted document with something left on it; pick one debit (invoice,
//   GRN) and one credit (receipt, payment, CN, fee) and knock them off against each other
// - statement: opening balance, every posted document in the period, running balance
// - each document row opens its knock-offs; hand-made ones can be undone

type PartyType = "customer" | "supplier";
interface Party { id: string; code: string; name: string }
interface OpenItem {
  doc_type: string; id: string; doc_no: string; doc_date: string; due_date: string | null;
  side: "D" | "C"; amount: string; settled: string; open: string;
}
interface StatementLine {
  doc_date: string; doc_type: string; id: string; doc_no: string;
  debit: string; credit: string; balance: string;
}
interface Statement { opening: string; lines: StatementLine[]; closing: string }
interface Knock {
  id: string; debit_type: string; debit_id: string; debit_no: string; credit_type: string;
  credit_id: string; credit_no: string; amount: string; source_type: string | null;
  source_id: string | null; created_at: string; voided_at: string | null;
}

// where each document type opens (fees have no screen of their own)
const ROUTE: Record<string, string> = {
  INV: "sales-invoice", CN: "customer-cn", OR: "receipt",
  GRN: "grn", SCN: "supplier-cn", PV: "supplier-payment",
};
const monthStart = () => today().slice(0, 8) + "01";

function DocLink({ type, id, no }: { type: string; id: string; no: string }) {
  const r = ROUTE[type];
  return r ? <Link to={`/docs/${r}/${id}`}>{no}</Link> : <span>{no}</span>;
}

export function Accounts() {
  const { t, can } = useSession();
  const [params, setParams] = useSearchParams();
  const type: PartyType = params.get("type") === "supplier" ? "supplier" : "customer";
  const partyId = params.get("party");
  const tab = params.get("tab") === "statement" ? "statement" : "open";
  const partyPath = type === "customer" ? "/customers" : "/suppliers";

  const [party, setParty] = useState<Party | null>(null);
  const [items, setItems] = useState<OpenItem[] | null>(null);
  const [balance, setBalance] = useState<string | null>(null);
  const [debit, setDebit] = useState<OpenItem | null>(null);
  const [credit, setCredit] = useState<OpenItem | null>(null);
  const [amount, setAmount] = useState("");
  const [stmt, setStmt] = useState<Statement | null>(null);
  const [from, setFrom] = useState(monthStart);
  const [to, setTo] = useState(today);
  const [opened, setOpened] = useState<string | null>(null);   // doc id whose knock-offs show
  const [knocks, setKnocks] = useState<Knock[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    setParams(next, { replace: true });
  };

  useEffect(() => {
    setParty(null);
    if (partyId) api<Party>(`${partyPath}/${partyId}`).then(setParty).catch(() => setParty(null));
  }, [partyId, partyPath]);

  const loadOpen = useCallback(() => {
    if (!partyId) return;
    setItems(null);
    api<{ items: OpenItem[]; balance: string }>("/finance/open-items",
      { query: { party_type: type, party_id: partyId } })
      .then((r) => { setItems(r.items); setBalance(r.balance); })
      .catch((e) => { setItems([]); setError(errorText(e)); });
  }, [partyId, type]);
  const loadStatement = useCallback(() => {
    if (!partyId) return;
    setStmt(null);
    api<Statement>("/finance/statement",
      { query: { party_type: type, party_id: partyId, date_from: from || null, date_to: to || null } })
      .then(setStmt)
      .catch((e) => setError(errorText(e)));
  }, [partyId, type, from, to]);

  useEffect(() => {
    setDebit(null);
    setCredit(null);
    setOpened(null);
    setError(null);
    setNotice(null);
    loadOpen();
  }, [loadOpen]);
  useEffect(() => {
    if (tab === "statement") loadStatement();
  }, [tab, loadStatement]);

  // what a knock-off of the two picked documents can be at most
  const most = debit && credit ? round2(Math.min(Number(debit.open), Number(credit.open))) : 0;
  useEffect(() => {
    setAmount(most ? most.toFixed(2) : "");
  }, [most]);

  const showKnocks = async (docId: string) => {
    if (opened === docId) return setOpened(null);
    setOpened(docId);
    setKnocks([]);
    setKnocks(await api<Knock[]>("/finance/knockoffs", { query: { doc_id: docId } }).catch(() => []));
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const knock = () => run(async () => {
    if (!debit || !credit) return;
    if (!isNum(amount) || Number(amount) <= 0 || Number(amount) > most + 0.001) {
      throw new Error(`${t("knockAmount")}: 0 – ${most.toFixed(2)}`);
    }
    const k = await api<Knock>("/finance/knockoffs", {
      method: "POST",
      body: { debit_type: debit.doc_type, debit_id: debit.id, credit_type: credit.doc_type,
              credit_id: credit.id, amount: amount.trim() },
    });
    setNotice(`${t("knocked")}: ${k.debit_no} ← ${k.credit_no} ${money(k.amount)}`);
    setDebit(null);
    setCredit(null);
    loadOpen();
  });
  const undo = (k: Knock) => run(async () => {
    if (!window.confirm(`${t("undoKnock")}: ${k.debit_no} ← ${k.credit_no} ${money(k.amount)}?`)) return;
    await api(`/finance/knockoffs/${k.id}`, { method: "DELETE" });
    setNotice(t("undone"));
    setKnocks(await api<Knock[]>("/finance/knockoffs", { query: { doc_id: opened as string } }).catch(() => []));
    loadOpen();
    if (tab === "statement") loadStatement();
  });

  const searchParties = useCallback(
    (q: string) => api<Page<Party>>(partyPath, { query: { q, page_size: 8 } }).then((p) => p.items),
    [partyPath]);

  if (!can("fin.doc.read")) {
    return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  }
  const debits = (items ?? []).filter((i) => i.side === "D");
  const credits = (items ?? []).filter((i) => i.side === "C");
  const canKnock = can("fin.doc.create");
  const bal = balance === null ? null : Number(balance);

  const knockList = (docId: string) => opened === docId && (
    <tr className="knock-row">
      <td colSpan={7}>
        {knocks.length === 0 ? <span className="muted">{t("noKnocks")}</span> : (
          <ul className="list">
            {knocks.map((k) => (
              <li key={k.id} className={k.voided_at ? "muted" : ""}>
                <span>
                  <DocLink type={k.debit_type} id={k.debit_id} no={k.debit_no} /> ←{" "}
                  <DocLink type={k.credit_type} id={k.credit_id} no={k.credit_no} />{" "}
                  <span className="muted">
                    {k.created_at.slice(0, 10)} · {k.source_type ? t("byDocument") : t("byHand")}
                    {k.voided_at ? ` · ${t("undone")}` : ""}
                  </span>
                </span>
                <span>
                  {money(k.amount)}{" "}
                  {canKnock && !k.voided_at && !k.source_id && (
                    <button className="chip-btn" disabled={busy} onClick={() => undo(k)}>{t("undoKnock")}</button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );

  const openTable = (rows: OpenItem[], picked: OpenItem | null, pick: (i: OpenItem | null) => void, title: Key) => (
    <section className="card glass">
      <h3>{t(title)}</h3>
      {rows.length === 0 ? <span className="muted">{t("nothingOpen")}</span> : (
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead>
              <tr>
                {canKnock && <th />}
                <th>{t("docNo")}</th><th>{t("date")}</th><th>{t("dueDate")}</th>
                <th className="num">{t("amount")}</th><th className="num">{t("openAmount")}</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <Fragment key={i.id}>
                  <tr className={picked?.id === i.id ? "picked" : ""}>
                    {canKnock && (
                      <td>
                        <input type="radio" name={title} checked={picked?.id === i.id}
                               aria-label={`${t("pick")} ${i.doc_no}`}
                               onChange={() => pick(i)} onClick={() => picked?.id === i.id && pick(null)} />
                      </td>
                    )}
                    <td><span className="muted">{i.doc_type}</span> <DocLink type={i.doc_type} id={i.id} no={i.doc_no} /></td>
                    <td>{i.doc_date}</td>
                    <td className={i.due_date && i.due_date < today() && i.side === "D" ? "error" : ""}>{i.due_date ?? ""}</td>
                    <td className="num">{money(i.amount)}</td>
                    <td className="num"><b>{money(i.open)}</b></td>
                    <td><button className="chip-btn" onClick={() => showKnocks(i.id)}>{t("knockoffs")}</button></td>
                  </tr>
                  {knockList(i.id)}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );

  return (
    <div className="docform accounts">
      <section className="card glass no-print">
        <div className="row" style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
          <div>
            <Link to="/finance" className="muted" style={{ textDecoration: "none" }}>← {t("finance")}</Link>
            <h2 style={{ margin: "4px 0 0" }}>{t("accounts")}</h2>
          </div>
          <div className="tabs" style={{ margin: 0 }}>
            {(["customer", "supplier"] as const).map((x) => (
              <button key={x} className={x === type ? "on" : ""}
                      onClick={() => set({ type: x, party: null })}>{t(x)}</button>
            ))}
          </div>
        </div>
        <div className="doc-head">
          <div className="field wide">
            <label htmlFor="party">{t(type)}</label>
            <Picker<Party> key={type} id="party" search={searchParties} keepText autoFocus={!partyId}
                    initial={party ? `${party.code} · ${party.name}` : ""}
                    label={(p) => `${p.code} · ${p.name}`} placeholder={t("searchParty")}
                    onPick={(p) => set({ party: p.id })} />
          </div>
          {bal !== null && partyId && (
            <div className="field">
              <label>{t(bal >= 0 ? (type === "customer" ? "customerOwes" : "weOwe")
                : (type === "customer" ? "customerCredit" : "supplierOwesUs"))}</label>
              <div className="big">{money(Math.abs(bal))}</div>
            </div>
          )}
        </div>
        {partyId && (
          <div className="tabs" style={{ margin: 0 }}>
            <button className={tab === "open" ? "on" : ""} onClick={() => set({ tab: null })}>{t("openItemsTab")}</button>
            <button className={tab === "statement" ? "on" : ""} onClick={() => set({ tab: "statement" })}>{t("statementTab")}</button>
          </div>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {notice && !error && <div className="muted" role="status">{notice}</div>}
      </section>

      {partyId && tab === "open" && (
        !items ? <section className="card glass"><span className="muted">{t("loading")}</span></section> : (
          <>
            {canKnock && (
              <section className="card glass knock-bar">
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <span>
                    {debit ? <b>{debit.doc_no}</b> : <span className="muted">{t("pickDebit")}</span>}
                    {" ← "}
                    {credit ? <b>{credit.doc_no}</b> : <span className="muted">{t("pickCredit")}</span>}
                  </span>
                  <div className="row" style={{ gap: 8 }}>
                    <input className="input sm num" inputMode="decimal" value={amount} aria-label={t("knockAmount")}
                           disabled={!debit || !credit} onChange={(e) => setAmount(e.target.value)} />
                    <button className="btn" disabled={busy || !debit || !credit} onClick={knock}>{t("knockOff")}</button>
                  </div>
                </div>
                <span className="muted">{t("knockHint")}</span>
              </section>
            )}
            <div className="open-pair">
              {openTable(debits, debit, setDebit, "openDebits")}
              {openTable(credits, credit, setCredit, "openCredits")}
            </div>
          </>
        )
      )}

      {partyId && tab === "statement" && (
        <section className="card glass statement">
          <div className="row no-print" style={{ flexWrap: "wrap" }}>
            <div className="row" style={{ gap: 8 }}>
              <input className="input sm" type="date" value={from} aria-label={t("dateFrom")}
                     onChange={(e) => setFrom(e.target.value)} />
              <span>–</span>
              <input className="input sm" type="date" value={to} aria-label={t("dateTo")}
                     onChange={(e) => setTo(e.target.value)} />
            </div>
            <button className="btn ghost" onClick={() => window.print()}>{t("printStatement")}</button>
          </div>
          <div className="print-only">
            <h2 style={{ margin: 0 }}>{t("statementTab")}</h2>
            <div>{party?.code} · {party?.name}</div>
            <div className="muted">{from || "…"} – {to || "…"}</div>
          </div>
          {!stmt ? <span className="muted">{t("loading")}</span> : (
            <div className="scroll-x">
              <table className="table doc-lines">
                <thead>
                  <tr>
                    <th>{t("date")}</th><th>{t("docNo")}</th>
                    <th className="num">{t("debitCol")}</th><th className="num">{t("creditCol")}</th>
                    <th className="num">{t("balanceCol")}</th><th className="no-print" /><th className="no-print" />
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{from}</td><td className="muted">{t("openingBalance")}</td><td /><td />
                    <td className="num"><b>{money(stmt.opening)}</b></td><td className="no-print" /><td className="no-print" />
                  </tr>
                  {stmt.lines.map((l) => (
                    <Fragment key={l.id}>
                      <tr>
                        <td>{l.doc_date}</td>
                        <td><span className="muted">{l.doc_type}</span> <DocLink type={l.doc_type} id={l.id} no={l.doc_no} /></td>
                        <td className="num">{Number(l.debit) ? money(l.debit) : ""}</td>
                        <td className="num">{Number(l.credit) ? money(l.credit) : ""}</td>
                        <td className="num">{money(l.balance)}</td>
                        <td className="no-print">
                          <button className="chip-btn" onClick={() => showKnocks(l.id)}>{t("knockoffs")}</button>
                        </td>
                        <td className="no-print" />
                      </tr>
                      {knockList(l.id)}
                    </Fragment>
                  ))}
                  <tr className="grand-row">
                    <td>{to}</td><td>{t("closingBalance")}</td><td /><td />
                    <td className="num"><b>{money(stmt.closing)}</b></td><td className="no-print" /><td className="no-print" />
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

