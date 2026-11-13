import { Fragment, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { DOC_ROUTE, errorText, isNum, today } from "../docs/shared";
import type { Key } from "../i18n";

// The books (DynaMod's accounting module): every posted document is booked to the general
// ledger by the server; here are the statements, the ledger of one account, the journals
// (with manual journal vouchers) and the chart of accounts.

type Tab = "tb" | "pl" | "bs" | "ledger" | "journals" | "accounts";
const TABS: { id: Tab; label: Key }[] = [
  { id: "pl", label: "accPL" }, { id: "bs", label: "accBS" }, { id: "tb", label: "accTB" },
  { id: "ledger", label: "accLedger" }, { id: "journals", label: "accJournals" },
  { id: "accounts", label: "accChart" },
];
const TYPES = ["ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"] as const;
// where a journal's source document opens
const SOURCE_ROUTE: Record<string, string> = {
  ...DOC_ROUTE, DO: "delivery-order", OR: "receipt",
};

interface Account { id: string; code: string; name: string; type: string; system_key: string | null; is_active: boolean; version: number }
interface Row { account_id: string; code: string; name: string; type: string; debit: string; credit: string; balance: string;
  debit_balance?: string; credit_balance?: string }
interface JLine { line_no: number; account_id: string; debit: string; credit: string; party_type: string | null; memo: string | null }
interface Journal {
  id: string; doc_no: string; journal_date: string; source_type: string; source_id: string | null; source_no: string | null;
  memo: string | null; is_reversal: boolean; status: string; lines: JLine[];
}

const startOfYear = () => `${today().slice(0, 4)}-01-01`;
const startOfMonth = () => `${today().slice(0, 7)}-01`;

export function Accounting() {
  const { t, can } = useSession();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.find((x) => x.id === params.get("tab"))?.id ?? "pl") as Tab;
  const [accounts, setAccounts] = useState<Account[]>([]);
  const loadAccounts = useCallback(() => {
    api<Page<Account>>("/accounting/accounts").then((p) => setAccounts(p.items)).catch(() => undefined);
  }, []);
  useEffect(loadAccounts, [loadAccounts]);
  if (!can("acc.read")) return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  const openLedger = (id: string) => setParams({ tab: "ledger", account: id });

  return (
    <div className="docform accounting">
      <section className="card glass no-print">
        <div className="row">
          <h2 style={{ margin: 0 }}>{t("accounting")}</h2>
          <button className="chip-btn" onClick={() => window.print()}>{t("printDoc")}</button>
        </div>
        <div className="tabs settings-tabs" style={{ margin: 0 }}>
          {TABS.map((x) => (
            <button key={x.id} className={x.id === tab ? "on" : ""}
                    onClick={() => setParams({ tab: x.id }, { replace: true })}>{t(x.label)}</button>
          ))}
        </div>
        <span className="muted">{t("accHint")}</span>
      </section>
      {tab === "pl" && <ProfitLoss onAccount={openLedger} />}
      {tab === "bs" && <BalanceSheet onAccount={openLedger} />}
      {tab === "tb" && <TrialBalance onAccount={openLedger} />}
      {tab === "ledger" && <Ledger accounts={accounts} account={params.get("account") ?? ""}
                                   onAccount={(id) => setParams({ tab: "ledger", account: id }, { replace: true })} />}
      {tab === "journals" && <Journals accounts={accounts} />}
      {tab === "accounts" && <Chart accounts={accounts} reload={loadAccounts} onAccount={openLedger} />}
    </div>
  );
}

function useReport<T>(path: string, query: Record<string, string>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = JSON.stringify(query);
  useEffect(() => {
    setData(null);
    setError(null);
    api<T>(path, { query }).then(setData).catch((e) => setError(errorText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key]);
  return { data, error };
}

function DateField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const id = `d-${label.replace(/\W+/g, "-")}`;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input id={id} className="input" type="date" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function Statement({ title, children, foot }: { title: string; children: ReactNode; foot?: ReactNode }) {
  return (
    <div className="scroll-x">
      <h3>{title}</h3>
      <table className="table doc-lines"><tbody>{children}</tbody>{foot && <tfoot>{foot}</tfoot>}</table>
    </div>
  );
}

function AccRows({ rows, onAccount }: { rows: Row[]; onAccount: (id: string) => void }) {
  return (
    <>
      {rows.map((r) => (
        <tr key={r.account_id}>
          <td className="nowrap"><button className="linkish" onClick={() => onAccount(r.account_id)}>{r.code}</button></td>
          <td>{r.name}</td>
          <td className="num">{money(r.balance)}</td>
        </tr>
      ))}
    </>
  );
}

const TotalRow = ({ label, value, strong }: { label: string; value: string | number; strong?: boolean }) => (
  <tr className={strong ? "grand-row" : ""}><td /><td><b>{label}</b></td><td className="num"><b>{money(value)}</b></td></tr>
);

function ProfitLoss({ onAccount }: { onAccount: (id: string) => void }) {
  const { t } = useSession();
  const [from, setFrom] = useState(startOfYear());
  const [to, setTo] = useState(today());
  const { data, error } = useReport<{
    income: Row[]; expense: Row[]; total_income: string; total_expense: string; net_sales: string;
    gross_profit: string; net_profit: string;
  }>("/accounting/profit-and-loss", { date_from: from, date_to: to });
  return (
    <section className="card glass">
      <div className="doc-head no-print">
        <DateField label={t("dateFrom")} value={from} onChange={setFrom} />
        <DateField label={t("dateTo")} value={to} onChange={setTo} />
      </div>
      <h2 className="print-only">{t("accPL")} · {from} – {to}</h2>
      {error && <div className="error">{error}</div>}
      {!data ? !error && <span className="muted">{t("loading")}</span> : (
        <>
          <div className="stat-row">
            <div><span className="muted">{t("netSales")}</span><b>{money(data.net_sales)}</b></div>
            <div><span className="muted">{t("grossProfit")}</span><b>{money(data.gross_profit)}</b></div>
            <div><span className="muted">{t("netProfit")}</span>
              <b className={Number(data.net_profit) < 0 ? "neg" : ""}>{money(data.net_profit)}</b></div>
          </div>
          <Statement title={t("accIncome")} foot={<TotalRow label={t("total")} value={data.total_income} />}>
            <AccRows rows={data.income} onAccount={onAccount} />
          </Statement>
          <Statement title={t("accExpense")} foot={<>
            <TotalRow label={t("total")} value={data.total_expense} />
            <TotalRow label={t("netProfit")} value={data.net_profit} strong />
          </>}>
            <AccRows rows={data.expense} onAccount={onAccount} />
          </Statement>
        </>
      )}
    </section>
  );
}

function BalanceSheet({ onAccount }: { onAccount: (id: string) => void }) {
  const { t } = useSession();
  const [asOf, setAsOf] = useState(today());
  const { data, error } = useReport<{
    assets: Row[]; liabilities: Row[]; equity: Row[]; profit_to_date: string; total_assets: string;
    total_liabilities: string; total_equity: string; balanced: boolean;
  }>("/accounting/balance-sheet", { as_of: asOf });
  return (
    <section className="card glass">
      <div className="doc-head no-print"><DateField label={t("asOf")} value={asOf} onChange={setAsOf} /></div>
      <h2 className="print-only">{t("accBS")} · {asOf}</h2>
      {error && <div className="error">{error}</div>}
      {!data ? !error && <span className="muted">{t("loading")}</span> : (
        <>
          {!data.balanced && <div className="error">{t("notBalanced")}</div>}
          <Statement title={t("accAssets")} foot={<TotalRow label={t("total")} value={data.total_assets} strong />}>
            <AccRows rows={data.assets} onAccount={onAccount} />
          </Statement>
          <Statement title={t("accLiabilities")} foot={<TotalRow label={t("total")} value={data.total_liabilities} />}>
            <AccRows rows={data.liabilities} onAccount={onAccount} />
          </Statement>
          <Statement title={t("accEquity")} foot={<>
            <TotalRow label={t("total")} value={data.total_equity} />
            <TotalRow label={t("liabPlusEquity")} value={Number(data.total_liabilities) + Number(data.total_equity)} strong />
          </>}>
            <AccRows rows={data.equity} onAccount={onAccount} />
            <tr><td /><td>{t("profitToDate")}</td><td className="num">{money(data.profit_to_date)}</td></tr>
          </Statement>
        </>
      )}
    </section>
  );
}

function TrialBalance({ onAccount }: { onAccount: (id: string) => void }) {
  const { t } = useSession();
  const [asOf, setAsOf] = useState(today());
  const { data, error } = useReport<{ rows: Row[]; total_debit: string; total_credit: string }>(
    "/accounting/trial-balance", { as_of: asOf });
  return (
    <section className="card glass">
      <div className="doc-head no-print"><DateField label={t("asOf")} value={asOf} onChange={setAsOf} /></div>
      <h2 className="print-only">{t("accTB")} · {asOf}</h2>
      {error && <div className="error">{error}</div>}
      {!data ? !error && <span className="muted">{t("loading")}</span> : (
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead><tr><th>{t("code")}</th><th>{t("name")}</th><th className="num">{t("debit")}</th><th className="num">{t("credit")}</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.account_id}>
                  <td><button className="linkish" onClick={() => onAccount(r.account_id)}>{r.code}</button></td>
                  <td>{r.name}</td>
                  <td className="num">{Number(r.debit_balance) ? money(r.debit_balance) : ""}</td>
                  <td className="num">{Number(r.credit_balance) ? money(r.credit_balance) : ""}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="grand-row"><td /><td><b>{t("total")}</b></td>
                <td className="num"><b>{money(data.total_debit)}</b></td><td className="num"><b>{money(data.total_credit)}</b></td></tr>
            </tfoot>
          </table>
        </div>
      )}
    </section>
  );
}

function SourceLink({ type, id, no }: { type: string; id: string | null; no: string | null }) {
  const route = SOURCE_ROUTE[type];
  if (!id || !route) return <>{no ?? "—"}</>;
  return <Link to={`/docs/${route}/${id}`}>{no}</Link>;
}

function Ledger({ accounts, account, onAccount }: { accounts: Account[]; account: string; onAccount: (id: string) => void }) {
  const { t } = useSession();
  const [from, setFrom] = useState(startOfMonth());
  const [to, setTo] = useState(today());
  const [data, setData] = useState<{
    opening: string; closing: string; lines: { journal_id: string; journal_no: string; date: string; source_type: string;
      source_id: string | null; source_no: string | null; memo: string | null; debit: string; credit: string; balance: string }[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    setError(null);
    if (!account) return;
    api<typeof data>(`/accounting/ledger/${account}`, { query: { date_from: from, date_to: to } })
      .then(setData).catch((e) => setError(errorText(e)));
  }, [account, from, to]);
  const acc = accounts.find((a) => a.id === account);
  return (
    <section className="card glass">
      <div className="doc-head no-print">
        <div className="field wide">
          <label htmlFor="l-acc">{t("account")}</label>
          <select id="l-acc" className="input" value={account} onChange={(e) => onAccount(e.target.value)}>
            <option value="">—</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name}</option>)}
          </select>
        </div>
        <DateField label={t("dateFrom")} value={from} onChange={setFrom} />
        <DateField label={t("dateTo")} value={to} onChange={setTo} />
      </div>
      <h2 className="print-only">{t("accLedger")} · {acc?.code} {acc?.name} · {from} – {to}</h2>
      {error && <div className="error">{error}</div>}
      {!account ? <span className="muted">{t("pickAccount")}</span> : !data ? !error && <span className="muted">{t("loading")}</span> : (
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead><tr><th>{t("date")}</th><th>{t("journalNo")}</th><th>{t("document")}</th><th>{t("remark")}</th>
              <th className="num">{t("debit")}</th><th className="num">{t("credit")}</th><th className="num">{t("balanceLeft")}</th></tr></thead>
            <tbody>
              <tr><td>{from}</td><td /><td /><td className="muted">{t("openingBalance")}</td><td /><td />
                <td className="num">{money(data.opening)}</td></tr>
              {data.lines.map((l, i) => (
                <tr key={`${l.journal_id}-${i}`}>
                  <td className="nowrap">{l.date}</td><td>{l.journal_no}</td>
                  <td><SourceLink type={l.source_type} id={l.source_id} no={l.source_no} /></td>
                  <td>{l.memo}</td>
                  <td className="num">{Number(l.debit) ? money(l.debit) : ""}</td>
                  <td className="num">{Number(l.credit) ? money(l.credit) : ""}</td>
                  <td className="num">{money(l.balance)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot><tr className="grand-row"><td /><td /><td /><td><b>{t("closingBalance")}</b></td><td /><td />
              <td className="num"><b>{money(data.closing)}</b></td></tr></tfoot>
          </table>
        </div>
      )}
    </section>
  );
}

interface JvRow { key: number; account_id: string; debit: string; credit: string; memo: string }
let jvKey = 1;
const jvRow = (): JvRow => ({ key: jvKey++, account_id: "", debit: "", credit: "", memo: "" });

function Journals({ accounts }: { accounts: Account[] }) {
  const { t, can } = useSession();
  const [from, setFrom] = useState(startOfMonth());
  const [to, setTo] = useState(today());
  const [rows, setRows] = useState<Journal[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jv, setJv] = useState<{ date: string; memo: string; lines: JvRow[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const name = (id: string) => { const a = accounts.find((x) => x.id === id); return a ? `${a.code} ${a.name}` : "…"; };
  const load = useCallback(() => {
    setRows(null);
    api<Page<Journal>>("/accounting/journals", { query: { date_from: from, date_to: to, page_size: 200 } })
      .then((p) => setRows(p.items)).catch((e) => { setRows([]); setError(errorText(e)); });
  }, [from, to]);
  useEffect(load, [load]);

  const usable = accounts.filter((a) => a.is_active && !["AR", "AP", "INVENTORY"].includes(a.system_key ?? ""));
  const sum = (k: "debit" | "credit") => (jv?.lines ?? []).reduce((s, l) => s + (isNum(l[k]) ? Number(l[k]) : 0), 0);
  const post = async () => {
    if (!jv) return;
    setBusy(true);
    setError(null);
    try {
      const lines = jv.lines.filter((l) => l.account_id && (l.debit.trim() || l.credit.trim()))
        .map((l) => ({ account_id: l.account_id, debit: l.debit.trim() || "0", credit: l.credit.trim() || "0", memo: l.memo.trim() || null }));
      await api("/accounting/journals", { method: "POST", body: { journal_date: jv.date, memo: jv.memo.trim(), lines } });
      setJv(null);
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const voidJv = async (j: Journal) => {
    const reason = window.prompt(t("voidReason"));
    if (!reason || reason.trim().length < 3) return;
    try {
      await api(`/accounting/journals/${j.id}/void`, { method: "POST", body: { reason: reason.trim() } });
      load();
    } catch (e) {
      setError(errorText(e));
    }
  };
  const setLine = (key: number, patch: Partial<JvRow>) =>
    setJv((x) => x && ({ ...x, lines: x.lines.map((l) => (l.key === key ? { ...l, ...patch } : l)) }));

  return (
    <section className="card glass">
      <div className="row no-print">
        <div className="doc-head" style={{ flex: 1 }}>
          <DateField label={t("dateFrom")} value={from} onChange={setFrom} />
          <DateField label={t("dateTo")} value={to} onChange={setTo} />
        </div>
        {can("acc.journal.create") && !jv && (
          <button className="btn" onClick={() => setJv({ date: today(), memo: "", lines: [jvRow(), jvRow()] })}>+ {t("newJv")}</button>
        )}
      </div>
      {jv && (
        <div className="pick-box no-print">
          <strong>{t("newJv")}</strong>
          <span className="muted">{t("jvHint")}</span>
          <div className="doc-head">
            <DateField label={t("date")} value={jv.date} onChange={(v) => setJv({ ...jv, date: v })} />
            <div className="field wide">
              <label htmlFor="jv-memo">{t("remark")}</label>
              <input id="jv-memo" className="input" value={jv.memo} maxLength={300}
                     onChange={(e) => setJv({ ...jv, memo: e.target.value })} />
            </div>
          </div>
          <div className="scroll-x">
            <table className="table doc-lines">
              <thead><tr><th>{t("account")}</th><th className="num">{t("debit")}</th><th className="num">{t("credit")}</th><th>{t("remark")}</th><th /></tr></thead>
              <tbody>
                {jv.lines.map((l) => (
                  <tr key={l.key}>
                    <td>
                      <select className="input sm" value={l.account_id} aria-label={t("account")}
                              onChange={(e) => setLine(l.key, { account_id: e.target.value })}>
                        <option value="">—</option>
                        {usable.map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name}</option>)}
                      </select>
                    </td>
                    <td className="num"><input className="input sm num" inputMode="decimal" value={l.debit} aria-label={t("debit")}
                               onChange={(e) => setLine(l.key, { debit: e.target.value, credit: e.target.value ? "" : l.credit })} /></td>
                    <td className="num"><input className="input sm num" inputMode="decimal" value={l.credit} aria-label={t("credit")}
                               onChange={(e) => setLine(l.key, { credit: e.target.value, debit: e.target.value ? "" : l.debit })} /></td>
                    <td><input className="input sm" value={l.memo} maxLength={200} aria-label={t("remark")}
                               onChange={(e) => setLine(l.key, { memo: e.target.value })} /></td>
                    <td><button className="chip-btn" aria-label={t("remove")}
                                onClick={() => setJv({ ...jv, lines: jv.lines.filter((x) => x.key !== l.key) })}>✕</button></td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><td><button className="chip-btn" onClick={() => setJv({ ...jv, lines: [...jv.lines, jvRow()] })}>+ {t("addLine")}</button></td>
                <td className="num"><b>{money(sum("debit"))}</b></td><td className="num"><b>{money(sum("credit"))}</b></td><td colSpan={2} /></tr></tfoot>
            </table>
          </div>
          <div className="actions">
            <button className="btn ghost" onClick={() => setJv(null)}>{t("cancel")}</button>
            <button className="btn green" disabled={busy || !jv.memo.trim() || Math.abs(sum("debit") - sum("credit")) > 0.001 || sum("debit") === 0}
                    onClick={post}>{t("post")}</button>
          </div>
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
      {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0 ? <span className="muted">{t("noResults")}</span> : (
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead><tr><th>{t("date")}</th><th>{t("journalNo")}</th><th>{t("document")}</th><th>{t("remark")}</th><th className="num">{t("amount")}</th><th /></tr></thead>
            <tbody>
              {rows.map((j) => (
                <Fragment key={j.id}>
                  <tr className="click" onClick={() => setOpen(open === j.id ? null : j.id)}>
                    <td className="nowrap">{j.journal_date}</td>
                    <td className="nowrap">{j.doc_no}{j.status === "VOIDED" && <span className="badge red"> VOID</span>}</td>
                    <td onClick={(e) => e.stopPropagation()}><SourceLink type={j.source_type} id={j.source_id} no={j.source_no} /></td>
                    <td>{j.memo}</td>
                    <td className="num">{money(j.lines.reduce((s, l) => s + Number(l.debit), 0))}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {j.source_type === "JV" && !j.is_reversal && j.status === "POSTED" && can("acc.journal.create") && (
                        <button className="chip-btn" onClick={() => voidJv(j)}>{t("voidDoc")}</button>
                      )}
                    </td>
                  </tr>
                  {open === j.id && j.lines.map((l) => (
                    <tr key={`${j.id}-${l.line_no}`} className="sub-row">
                      <td /><td colSpan={2} className="muted">{name(l.account_id)}</td>
                      <td className="muted">{l.memo}</td>
                      <td className="num muted">{Number(l.debit) ? `Dr ${money(l.debit)}` : `Cr ${money(l.credit)}`}</td><td />
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Chart({ accounts, reload, onAccount }: { accounts: Account[]; reload: () => void; onAccount: (id: string) => void }) {
  const { t, can } = useSession();
  const [f, setF] = useState({ code: "", name: "", type: "EXPENSE" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const manage = can("acc.account.manage");
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try { await fn(); reload(); return true; } catch (e) { setError(errorText(e)); return false; } finally { setBusy(false); }
  };
  return (
    <section className="card glass">
      {TYPES.map((type) => (
        <div key={type} className="scroll-x">
          <h3>{t(`accType${type}` as Key)}</h3>
          <table className="table one-line">
            <tbody>
              {accounts.filter((a) => a.type === type).map((a) => (
                <tr key={a.id} className={a.is_active ? "" : "muted"}>
                  <td style={{ width: 90 }}><button className="linkish" onClick={() => onAccount(a.id)}>{a.code}</button></td>
                  <td>{a.name}</td>
                  <td className="muted">{a.system_key ? t("systemAccount") : ""}</td>
                  <td style={{ width: 110 }}>
                    {manage && !a.system_key && (
                      <button className="chip-btn" disabled={busy}
                              onClick={() => run(() => api(`/accounting/accounts/${a.id}`, {
                                method: "PATCH", body: { version: a.version, is_active: !a.is_active } }))}>
                        {t(a.is_active ? "deactivate" : "activate")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {manage && (
        <form className="add-row" onSubmit={async (e) => {
          e.preventDefault();
          if (await run(() => api("/accounting/accounts", { method: "POST", body: { ...f, code: f.code.trim(), name: f.name.trim() } }))) {
            setF({ code: "", name: "", type: f.type });
          }
        }}>
          <div className="doc-head">
            <div className="field"><label htmlFor="a-code">{t("code")}</label>
              <input id="a-code" className="input" value={f.code} maxLength={20} onChange={(e) => setF({ ...f, code: e.target.value })} /></div>
            <div className="field wide"><label htmlFor="a-name">{t("name")}</label>
              <input id="a-name" className="input" value={f.name} maxLength={100} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
            <div className="field"><label htmlFor="a-type">{t("accountType")}</label>
              <select id="a-type" className="input" value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
                {TYPES.map((x) => <option key={x} value={x}>{t(`accType${x}` as Key)}</option>)}
              </select></div>
          </div>
          <button className="btn" disabled={busy || !f.code.trim() || /\s/.test(f.code.trim()) || !f.name.trim()}>+ {t("add")}</button>
        </form>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}
