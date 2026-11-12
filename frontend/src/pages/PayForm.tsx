import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { STATUS_COLOR } from "../docs/kinds";
import type { KindDef } from "../docs/kinds";
import { errorText, isNum, round2, today, warnText } from "../docs/shared";
import type { Warning } from "../docs/shared";
import type { Key } from "../i18n";

// Customer receipts (OR) and supplier payments (PV).
// Receipt: the money received, and which open invoices it pays; whatever is not assigned
// either pays the oldest invoices on posting (auto) or stays as an advance.
// Payment: keyed in line by line as the supplier's payment advice reads; on save the server
// matches each line to our GRN / supplier CN by the supplier's own number.

interface Party { id: string; code: string; name: string }
interface OpenItem {
  doc_type: string; id: string; doc_no: string; doc_date: string; due_date: string | null;
  side: string; amount: string; settled: string; open: string;
}
interface PayLine {
  id: string; line_no: number; amount: string; invoice_id?: string | null; line_type?: string | null;
  ref_no?: string | null; fee_type?: string | null; tax_code_id?: string | null;
  matched_no?: string | null; match_status?: string | null; remark?: string | null;
}
interface PayDoc {
  id: string; doc_no: string | null; doc_date: string; status: string; version: number;
  customer_id?: string | null; supplier_id?: string | null; location_id: string;
  method: string; bank_ref: string | null; amount: string; unallocated?: string | null;
  auto_allocate?: boolean | null; supplier_ref_no?: string | null; remark: string | null;
  void_reason: string | null; lines: PayLine[]; warnings?: Warning[];
}
interface Knock {
  id: string; debit_type: string; debit_no: string; credit_type: string; credit_no: string;
  amount: string; voided_at: string | null;
}
interface TaxCode { id: string; code: string; rate: string; is_active: boolean }
interface Head {
  doc_date: string; party: Party | null; location_id: string; method: string; bank_ref: string;
  remark: string; amount: string; auto_allocate: boolean; supplier_ref_no: string;
}
interface PRow {
  key: number; line_type: string; ref_no: string; fee_type: string; tax_code_id: string;
  amount: string; remark: string; matched_no?: string | null; match_status?: string | null;
}

const METHODS = ["TRANSFER", "CASH", "CHEQUE", "CARD", "EWALLET", "OTHER"] as const;
const LINE_TYPES = ["INVOICE", "CN", "FEE", "OTHER"] as const;
const FEE_TYPES = ["FREIGHT", "LISTING", "PROMOTION", "DISPLAY", "REBATE", "OTHER"] as const;
const SIGN: Record<string, number> = { INVOICE: 1, CN: -1, FEE: -1, OTHER: 1 };
const MATCH_COLOR: Record<string, string> = {
  MATCHED: "green", AMOUNT_DIFF: "amber", NOT_READY: "amber", NOT_FOUND: "red",
};
let nextKey = 1;

const emptyHead = (): Head => ({
  doc_date: today(), party: null, location_id: "", method: "TRANSFER", bank_ref: "", remark: "",
  amount: "", auto_allocate: true, supplier_ref_no: "",
});

export function PayForm({ def, id }: { def: KindDef; id: string }) {
  const { t, can } = useSession();
  const nav = useNavigate();
  const receipt = def.kind === "receipt";
  const partyPath = receipt ? "/customers" : "/suppliers";
  const listPath = `/finance?kind=${def.kind}`;

  const [doc, setDoc] = useState<PayDoc | null>(null);
  const [head, setHead] = useState<Head>(emptyHead);
  const [alloc, setAlloc] = useState<Record<string, string>>({});   // receipt: invoice id -> amount
  const [rows, setRows] = useState<PRow[]>([]);                      // payment lines
  const [open, setOpen] = useState<OpenItem[]>([]);
  const [balance, setBalance] = useState<string | null>(null);
  const [invNo, setInvNo] = useState<Record<string, string>>({});
  const [knocks, setKnocks] = useState<Knock[]>([]);
  const [taxes, setTaxes] = useState<TaxCode[]>([]);
  const [locations, setLocations] = useState<{ id: string; code: string; name: string }[]>([]);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(id !== "new");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [voiding, setVoiding] = useState(false);
  const [voidReason, setVoidReason] = useState("");

  const editable = (!doc || doc.status === "DRAFT" || doc.status === "SAVED") && can("fin.doc.create");

  // --- loading -----------------------------------------------------------------------------

  useEffect(() => {
    api<Page<TaxCode>>("/tax-codes").then((p) => setTaxes(p.items)).catch(() => undefined);
    api<Page<{ id: string; code: string; name: string }>>("/locations")
      .then((p) => {
        setLocations(p.items);
        setHead((h) => (h.location_id || !p.items[0] ? h : { ...h, location_id: p.items[0].id }));
      })
      .catch(() => undefined);
  }, []);

  const apply = useCallback(async (d: PayDoc) => {
    const partyId = (d.customer_id ?? d.supplier_id) as string;
    const party = await api<Party>(`${partyPath}/${partyId}`).catch(() => null);
    setDoc(d);
    setHead({
      doc_date: d.doc_date, party: party ?? { id: partyId, code: "", name: "…" },
      location_id: d.location_id, method: d.method, bank_ref: d.bank_ref ?? "", remark: d.remark ?? "",
      amount: String(Number(d.amount)), auto_allocate: d.auto_allocate ?? true,
      supplier_ref_no: d.supplier_ref_no ?? "",
    });
    if (receipt) {
      const a: Record<string, string> = {};
      d.lines.forEach((l) => { if (l.invoice_id) a[l.invoice_id] = String(Number(l.amount)); });
      setAlloc(a);
      // invoice numbers for lines whose invoice is no longer open
      const ids = d.lines.map((l) => l.invoice_id).filter(Boolean) as string[];
      Promise.all(ids.map((i) => api<{ id: string; doc_no: string }>(`/sales/sales-invoice/${i}`)
        .catch(() => null)))
        .then((r) => setInvNo((m) => ({ ...m, ...Object.fromEntries(
          r.filter(Boolean).map((x) => [x!.id, x!.doc_no])) })));
    } else {
      setRows(d.lines.map((l) => ({
        key: nextKey++, line_type: l.line_type ?? "INVOICE", ref_no: l.ref_no ?? "",
        fee_type: l.fee_type ?? "", tax_code_id: l.tax_code_id ?? "", amount: String(Number(l.amount)),
        remark: l.remark ?? "", matched_no: l.matched_no, match_status: l.match_status,
      })));
    }
    setWarnings(d.warnings ?? []);
    setKnocks(d.status === "POSTED"
      ? await api<Knock[]>("/finance/knockoffs", { query: { doc_id: d.id } }).catch(() => [])
      : []);
    setDirty(false);
  }, [partyPath, receipt]);

  useEffect(() => {
    if (id === "new") {
      if (doc) {
        setDoc(null);
        setHead((h) => ({ ...emptyHead(), location_id: h.location_id }));
        setAlloc({});
        setRows([]);
        setWarnings([]);
        setKnocks([]);
        setError(null);
        setNotice(null);
        setDirty(false);
      }
      return;
    }
    if (doc?.id === id) return;
    setLoading(true);
    api<PayDoc>(`${def.api}/${id}`)
      .then((d) => apply(d))
      .catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // the party's open documents: what a receipt can pay, what a payment advice refers to
  const loadOpen = useCallback(() => {
    if (!head.party) return;
    api<{ items: OpenItem[]; balance: string }>("/finance/open-items",
      { query: { party_type: def.party, party_id: head.party.id } })
      .then((r) => {
        setOpen(r.items);
        setBalance(r.balance);
        setInvNo((m) => ({ ...m, ...Object.fromEntries(r.items.map((i) => [i.id, i.doc_no])) }));
      })
      .catch(() => { setOpen([]); setBalance(null); });
  }, [def.party, head.party]);
  useEffect(() => {
    setOpen([]);
    setBalance(null);
    loadOpen();
  }, [loadOpen]);

  // --- editing -----------------------------------------------------------------------------

  const setH = (patch: Partial<Head>) => { setHead((h) => ({ ...h, ...patch })); setDirty(true); };
  const setA = (invoiceId: string, v: string) => { setAlloc((a) => ({ ...a, [invoiceId]: v })); setDirty(true); };
  const setRow = (key: number, patch: Partial<PRow>) => {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch, match_status: undefined } : r)));
    setDirty(true);
  };
  const addRow = (line_type: string, extra: Partial<PRow> = {}) => {
    setRows((rs) => [...rs, {
      key: nextKey++, line_type, ref_no: "", fee_type: line_type === "FEE" ? "OTHER" : "",
      tax_code_id: "", amount: "", remark: "", ...extra,
    }]);
    setDirty(true);
  };
  const searchParties = useCallback(
    (q: string) => api<Page<Party>>(partyPath, { query: { q, page_size: 8 } }).then((p) => p.items),
    [partyPath]);

  const invoices = open.filter((i) => i.doc_type === "INV" && i.side === "D");
  const supplierDocs = open.filter((i) => i.doc_type === "GRN" || i.doc_type === "SCN");

  /** Receipt: spread the amount over the oldest open invoices (due date, then date). */
  const spread = () => {
    let left = Number(head.amount) || 0;
    const a: Record<string, string> = {};
    [...invoices]
      .sort((x, y) => (x.due_date ?? x.doc_date).localeCompare(y.due_date ?? y.doc_date))
      .forEach((i) => {
        const pay = round2(Math.min(left, Number(i.open)));
        if (pay > 0) a[i.id] = pay.toFixed(2);
        left = round2(left - pay);
      });
    setAlloc(a);
    setDirty(true);
  };

  /** Payment: add an open GRN / supplier CN, keyed by the supplier's own number. */
  const addOpenDoc = async (i: OpenItem) => {
    const path = i.doc_type === "GRN" ? "/purchase/grn" : "/purchase/supplier-cn";
    const d = await api<{ supplier_ref_no: string | null }>(`${path}/${i.id}`).catch(() => null);
    addRow(i.doc_type === "GRN" ? "INVOICE" : "CN",
      { ref_no: d?.supplier_ref_no ?? "", amount: Number(i.open).toFixed(2) });
    if (!d?.supplier_ref_no) setNotice(`${i.doc_no}: ${t("noSupplierRef")}`);
  };

  // --- numbers -----------------------------------------------------------------------------

  const allocated = useMemo(
    () => round2(Object.values(alloc).reduce((s, v) => s + (isNum(v) ? Number(v) : 0), 0)), [alloc]);
  const net = useMemo(
    () => round2(rows.reduce((s, r) => s + (isNum(r.amount) ? SIGN[r.line_type] * Number(r.amount) : 0), 0)),
    [rows]);

  // --- talking to the server -----------------------------------------------------------------

  const check = (): string | null => {
    if (!head.party) return t("pickParty");
    if (receipt) {
      if (!isNum(head.amount) || Number(head.amount) <= 0) return t("amountReceived");
      if (allocated > Number(head.amount) + 0.001) return t("overAllocated");
      for (const [inv, v] of Object.entries(alloc)) {
        if (v.trim() !== "" && !isNum(v)) return `${invNo[inv] ?? ""}: ${t("thisPay")}`;
      }
    } else {
      for (const [i, r] of rows.entries()) {
        const at = t("lineNo").replace("{n}", String(i + 1));
        if (!isNum(r.amount)) return `${at}: ${t("amount")}`;
        if ((r.line_type === "INVOICE" || r.line_type === "CN") && !r.ref_no.trim()) return `${at}: ${t("supplierDocNo")}`;
      }
    }
    return null;
  };

  const body = () => receipt ? {
    doc_date: head.doc_date, customer_id: head.party?.id, location_id: head.location_id,
    method: head.method, bank_ref: head.bank_ref.trim() || null, remark: head.remark.trim() || null,
    amount: head.amount.trim(), auto_allocate: head.auto_allocate,
    lines: Object.entries(alloc).filter(([, v]) => isNum(v) && Number(v) > 0)
      .map(([invoice_id, amount]) => ({ invoice_id, amount: amount.trim() })),
  } : {
    doc_date: head.doc_date, supplier_id: head.party?.id, location_id: head.location_id,
    method: head.method, bank_ref: head.bank_ref.trim() || null, remark: head.remark.trim() || null,
    supplier_ref_no: head.supplier_ref_no.trim() || null,
    lines: rows.map((r) => ({
      line_type: r.line_type, ref_no: r.ref_no.trim() || null, amount: r.amount.trim(),
      fee_type: r.line_type === "FEE" ? r.fee_type || "OTHER" : null,
      tax_code_id: r.line_type === "FEE" ? r.tax_code_id || null : null,
      remark: r.remark.trim() || null,
    })),
  };

  const persist = async (): Promise<PayDoc> => {
    const bad = check();
    if (bad) throw new Error(bad);
    if (!doc) {
      const d = await api<PayDoc>(def.api, { method: "POST", body: body() });
      await apply(d);
      nav(`/docs/${def.kind}/${d.id}`, { replace: true });
      return d;
    }
    if (!dirty) return doc;
    const d = await api<PayDoc>(`${def.api}/${doc.id}`,
      { method: "PATCH", body: { version: doc.version, ...body() } });
    await apply(d);
    return d;
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
  const step = (path: string, extra: Record<string, unknown> = {}) => run(async () => {
    const d = await persist();
    const after = await api<PayDoc>(`${def.api}/${d.id}/${path}`,
      { method: "POST", body: { version: d.version, ...extra } });
    await apply(after);
    loadOpen();
    setNotice(t("saved"));
  });
  const saveDraft = () => run(async () => { await persist(); setNotice(t("saved")); });
  const remove = () => run(async () => {
    if (!doc || !window.confirm(t("confirmDelete"))) return;
    await api(`${def.api}/${doc.id}`, { method: "DELETE" });
    nav(listPath);
  });

  // --- screen -------------------------------------------------------------------------------

  if (loading) return <section className="card glass"><span className="muted">{t("loading")}</span></section>;
  if (loadError || (!doc && !can("fin.doc.create"))) {
    return (
      <section className="card glass">
        <Link to={listPath} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
        <div className="error" role="alert">{loadError ?? t("noAccess")}</div>
      </section>
    );
  }
  const status = doc?.status ?? "NEW";
  const amount = Number(head.amount) || 0;
  const left = round2(amount - allocated);
  // a receipt being edited may name invoices that are not open any more (posted ones)
  const shownInvoices: { id: string; doc_no: string; doc_date?: string; due_date?: string | null; open?: string }[] =
    editable ? [
      ...invoices,
      ...Object.keys(alloc).filter((i) => !invoices.some((x) => x.id === i))
        .map((i) => ({ id: i, doc_no: invNo[i] ?? "…" })),
    ] : (doc?.lines ?? []).map((l) => ({ id: l.invoice_id as string, doc_no: invNo[l.invoice_id as string] ?? "…" }));

  return (
    <div className="docform">
      <section className="card glass">
        <div className="row">
          <div>
            <Link to={listPath} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
            <h2 style={{ margin: "4px 0 0" }}>
              {t(def.kind)} {doc?.doc_no ?? <span className="muted">{t("draftNo")}</span>}
            </h2>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {dirty && <span className="badge amber">{t("unsaved")}</span>}
            {!editable && doc && <span className="muted nowrap">{t("readOnly")}</span>}
            <span className={`badge ${STATUS_COLOR[status] ?? ""}`}>{status}</span>
          </div>
        </div>

        <div className="doc-head">
          <div className="field wide">
            <label htmlFor="party">{t(def.party)}</label>
            <Picker<Party> id="party" search={searchParties} keepText disabled={!editable}
                    initial={head.party ? [head.party.code, head.party.name].filter(Boolean).join(" · ") : ""}
                    label={(p) => `${p.code} · ${p.name}`} placeholder={t("searchParty")}
                    onPick={(p) => { setH({ party: p }); setAlloc({}); }} autoFocus={!doc} />
            {balance !== null && (
              <span className="muted">
                {/* positive: they owe / we owe; negative: money sits on the other side */}
                {t(Number(balance) >= 0 ? (receipt ? "customerOwes" : "weOwe")
                  : (receipt ? "customerCredit" : "supplierOwesUs"))}: {money(Math.abs(Number(balance)))}
                {head.party && (
                  <> · <Link to={`/accounts?type=${def.party}&party=${head.party.id}`}>{t("accounts")}</Link></>
                )}
              </span>
            )}
          </div>
          <div className="field">
            <label htmlFor="docdate">{t("date")}</label>
            <input id="docdate" className="input" type="date" value={head.doc_date} disabled={!editable}
                   onChange={(e) => setH({ doc_date: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="loc">{t("store")}</label>
            <select id="loc" className="input" value={head.location_id} disabled={!editable}
                    onChange={(e) => setH({ location_id: e.target.value })}>
              {locations.map((l) => <option key={l.id} value={l.id}>{l.code} · {l.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="method">{t("method")}</label>
            <select id="method" className="input" value={head.method} disabled={!editable}
                    onChange={(e) => setH({ method: e.target.value })}>
              {METHODS.map((m) => <option key={m} value={m}>{t(`method${m}` as Key)}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="bankref">{t("bankRef")}</label>
            <input id="bankref" className="input" value={head.bank_ref} disabled={!editable} maxLength={50}
                   onChange={(e) => setH({ bank_ref: e.target.value })} />
          </div>
          {receipt ? (
            <div className="field">
              <label htmlFor="amount">{t("amountReceived")}</label>
              <input id="amount" className="input num-wide" inputMode="decimal" value={head.amount}
                     disabled={!editable} onChange={(e) => setH({ amount: e.target.value })} />
            </div>
          ) : (
            <div className="field">
              <label htmlFor="advice">{t("adviceNo")}</label>
              <input id="advice" className="input" value={head.supplier_ref_no} disabled={!editable}
                     maxLength={50} onChange={(e) => setH({ supplier_ref_no: e.target.value })} />
            </div>
          )}
          <div className="field wide">
            <label htmlFor="remark">{t("remark")}</label>
            <input id="remark" className="input" value={head.remark} disabled={!editable} maxLength={500}
                   onChange={(e) => setH({ remark: e.target.value })} />
          </div>
        </div>
        {doc?.void_reason && <span className="error">{t("voidDoc")}: {doc.void_reason}</span>}
      </section>

      {receipt ? (
        <section className="card glass">
          <div className="row">
            <h3 style={{ margin: 0 }}>{t(editable ? "openInvoices" : "paidInvoices")}</h3>
            {editable && invoices.length > 0 && (
              <button className="chip-btn" onClick={spread} disabled={!amount}>{t("spreadOldest")}</button>
            )}
          </div>
          {!head.party ? <span className="muted">{t("pickParty")}</span>
            : shownInvoices.length === 0 ? <span className="muted">{t("noOpenInvoices")}</span> : (
              <div className="scroll-x">
                <table className="table doc-lines">
                  <thead>
                    <tr>
                      <th>{t("docNo")}</th><th>{t("date")}</th><th>{t("dueDate")}</th>
                      <th className="num">{t("openAmount")}</th><th className="num">{t("thisPay")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownInvoices.map((i) => (
                      <tr key={i.id}>
                        <td>{i.doc_no}</td>
                        <td>{i.doc_date ?? ""}</td>
                        <td>{i.due_date ?? ""}</td>
                        <td className="num">{i.open !== undefined ? money(i.open) : ""}</td>
                        <td className="num">
                          <input className="input sm num" inputMode="decimal" value={alloc[i.id] ?? ""}
                                 disabled={!editable} aria-label={`${t("thisPay")} ${i.doc_no}`}
                                 onChange={(e) => setA(i.id, e.target.value)} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          {editable && (
            <label className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
              <input type="checkbox" checked={head.auto_allocate}
                     onChange={(e) => setH({ auto_allocate: e.target.checked })} />
              <span>{t("autoAllocate")}</span>
            </label>
          )}
          <div className="doc-foot">
            <div className="totals">
              <div><span>{t("amountReceived")}</span><b>{money(amount)}</b></div>
              <div><span>{t("assigned")}</span><b>{money(allocated)}</b></div>
              {doc?.status === "POSTED" ? (
                <div className="grand"><span>{t("advance")}</span><b>{money(doc.unallocated)}</b></div>
              ) : (
                <div className="grand">
                  <span>{left < 0 ? t("overAllocated") : head.auto_allocate ? t("leftAuto") : t("advance")}</span>
                  <b className={left < 0 ? "error" : ""}>{money(left)}</b>
                </div>
              )}
            </div>
            {actions()}
          </div>
          {messages()}
        </section>
      ) : (
        <section className="card glass">
          <h3>{t("adviceLines")}</h3>
          <div className="scroll-x">
            <table className="table doc-lines">
              <thead>
                <tr>
                  <th>#</th><th>{t("lineType")}</th><th>{t("supplierDocNo")}</th><th>{t("feeType")}</th>
                  <th className="num">{t("amount")}</th><th>{t("matched")}</th>{editable && <th />}
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && <tr><td colSpan={7} className="muted">{t("noAdviceLines")}</td></tr>}
                {rows.map((r, i) => (
                  <tr key={r.key}>
                    <td className="muted">{i + 1}</td>
                    <td>
                      <select className="input sm" value={r.line_type} disabled={!editable} aria-label={t("lineType")}
                              onChange={(e) => setRow(r.key, { line_type: e.target.value,
                                fee_type: e.target.value === "FEE" ? r.fee_type || "OTHER" : "" })}>
                        {LINE_TYPES.map((x) => <option key={x} value={x}>{t(`line${x}` as Key)}</option>)}
                      </select>
                    </td>
                    <td>
                      <input className="input sm" value={r.ref_no} disabled={!editable} maxLength={50}
                             aria-label={t("supplierDocNo")}
                             placeholder={r.line_type === "FEE" || r.line_type === "OTHER" ? t("optional") : ""}
                             onChange={(e) => setRow(r.key, { ref_no: e.target.value })} />
                    </td>
                    <td>
                      {r.line_type === "FEE" ? (
                        <div className="row" style={{ gap: 6, justifyContent: "flex-start" }}>
                          <select className="input sm" value={r.fee_type} disabled={!editable} aria-label={t("feeType")}
                                  onChange={(e) => setRow(r.key, { fee_type: e.target.value })}>
                            {FEE_TYPES.map((x) => <option key={x} value={x}>{t(`fee${x}` as Key)}</option>)}
                          </select>
                          <select className="input sm" value={r.tax_code_id} disabled={!editable} aria-label={t("tax")}
                                  onChange={(e) => setRow(r.key, { tax_code_id: e.target.value })}>
                            <option value="">{t("noTax")}</option>
                            {taxes.filter((x) => x.is_active || x.id === r.tax_code_id).map((x) => (
                              <option key={x.id} value={x.id}>{x.code}</option>
                            ))}
                          </select>
                        </div>
                      ) : <span className="muted">—</span>}
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={r.amount} disabled={!editable}
                             aria-label={t("amount")} onChange={(e) => setRow(r.key, { amount: e.target.value })} />
                    </td>
                    <td>
                      {r.match_status
                        ? <span className={`badge ${MATCH_COLOR[r.match_status] ?? ""}`}>
                            {r.matched_no ? `${r.matched_no} · ` : ""}{t(`match${r.match_status}` as Key)}
                          </span>
                        : <span className="muted">{SIGN[r.line_type] < 0 ? t("deducted") : ""}</span>}
                    </td>
                    {editable && (
                      <td>
                        <button className="chip-btn" aria-label={t("remove")}
                                onClick={() => { setRows((rs) => rs.filter((x) => x.key !== r.key)); setDirty(true); }}>
                          ✕
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {editable && (
            <div className="row" style={{ justifyContent: "flex-start", flexWrap: "wrap", gap: 6 }}>
              {LINE_TYPES.map((x) => (
                <button key={x} className="chip-btn" onClick={() => addRow(x)}>+ {t(`line${x}` as Key)}</button>
              ))}
            </div>
          )}
          {editable && head.party && supplierDocs.length > 0 && (
            <details className="open-docs">
              <summary>{t("openSupplierDocs")} ({supplierDocs.length})</summary>
              <ul className="list">
                {supplierDocs.map((i) => (
                  <li key={i.id}>
                    <span>{i.doc_no} · {i.doc_date}</span>
                    <span>
                      {money(i.open)}{" "}
                      <button className="chip-btn" onClick={() => addOpenDoc(i)}>{t("addToAdvice")}</button>
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <div className="doc-foot">
            <div className="totals">
              <div className="grand">
                <span>{net < 0 ? t("supplierPaysUs") : t("netPaid")}</span>
                <b>{money(Math.abs(doc && !dirty ? Number(doc.amount) : net))}</b>
              </div>
              {doc?.status === "POSTED" && doc.unallocated != null && Number(doc.unallocated) !== 0 && (
                <div><span>{t("notKnocked")}</span><b>{money(doc.unallocated)}</b></div>
              )}
            </div>
            {actions()}
          </div>
          {messages()}
        </section>
      )}

      {knocks.length > 0 && (
        <section className="card glass">
          <h3>{t("knockoffs")}</h3>
          <ul className="list">
            {knocks.filter((k) => !k.voided_at).map((k) => (
              <li key={k.id}><span>{k.debit_no} ← {k.credit_no}</span><b>{money(k.amount)}</b></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );

  function actions() {
    return (
      <div className="actions">
        {editable && status !== "SAVED" && (
          <button className="btn ghost" disabled={busy} onClick={saveDraft}>{t("saveDraft")}</button>
        )}
        {editable && status !== "SAVED" && (
          <button className="btn" disabled={busy} onClick={() => step("save")}>{t("saveDoc")}</button>
        )}
        {editable && status === "SAVED" && (
          <button className="btn ghost" disabled={busy || !dirty} onClick={saveDraft}>{t("update")}</button>
        )}
        {status === "SAVED" && can("fin.doc.post") && (
          <button className="btn green" disabled={busy} onClick={() => step("post")}>{t("post")}</button>
        )}
        {doc && (status === "SAVED" || status === "POSTED") && can("fin.doc.void") && (
          <button className="btn ghost danger" disabled={busy} onClick={() => setVoiding((v) => !v)}>{t("voidDoc")}</button>
        )}
        {status === "DRAFT" && editable && (
          <button className="btn ghost danger" disabled={busy} onClick={remove}>{t("deleteDraft")}</button>
        )}
      </div>
    );
  }

  function messages() {
    return (
      <>
        {voiding && doc && (
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <input className="input" style={{ flex: 1, maxWidth: 420 }} value={voidReason}
                   placeholder={t("voidReason")} onChange={(e) => setVoidReason(e.target.value)} />
            <button className="btn danger-fill" disabled={busy || voidReason.trim().length < 3}
                    onClick={() => step("void", { reason: voidReason.trim() }).then(() => setVoiding(false))}>
              {t("voidDoc")}
            </button>
          </div>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {notice && !error && <div className="muted" role="status">{notice}</div>}
        {warnings.length > 0 && (
          <div className="warn-box">
            <b>{t("warnings")}</b>
            <ul>{warnings.map((w, i) => <li key={i}>{warnText(t, w)}</li>)}</ul>
          </div>
        )}
      </>
    );
  }
}
