import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { STATUS_COLOR } from "../docs/kinds";
import type { KindDef } from "../docs/kinds";
import { amt, errorText, isNum, round2, today, warnText } from "../docs/shared";
import type { Warning } from "../docs/shared";
import { PrintSheet } from "../docs/PrintSheet";
import type { PrintParty } from "../docs/PrintSheet";
import type { Key } from "../i18n";

// Supplier documents that move no stock (DynaMod's supplier module):
// - debit note: the supplier charges more (freight, under-billing) - paid like an invoice;
// - credit note without goods: price adjustment, rebate - deducted from a payment;
// - refund: money the supplier pays back, settling their CNs / our overpayment.
// Notes have description + amount + tax lines; a refund has an amount and (optionally) the
// supplier credits it settles - left empty, the oldest open credits are taken on posting.

interface Party extends PrintParty { id: string; code: string; name: string }
interface Line {
  id: string; line_no: number; description?: string | null; ref_doc_no?: string | null;
  tax_code_id?: string | null; line_tax?: string | null; line_total?: string | null;
  doc_type?: string | null; doc_id?: string | null; doc_no?: string | null; amount: string;
}
interface NoteDoc {
  id: string; doc_no: string | null; doc_date: string; status: string; version: number;
  supplier_id?: string; customer_id?: string; location_id: string;
  supplier_ref_no?: string | null; customer_ref_no?: string | null; remark: string | null;
  subtotal?: string | null; tax_total?: string | null; total?: string | null;
  amount?: string | null; method?: string | null; bank_ref?: string | null; open?: string | null;
  void_reason: string | null; lines: Line[]; warnings?: Warning[];
}
interface OpenItem { doc_type: string; id: string; doc_no: string; doc_date: string; side: string; open: string }
interface TaxCode { id: string; code: string; rate: string; is_active: boolean }
interface Row { key: number; description: string; ref_doc_no: string; tax_code_id: string; amount: string }

const METHODS = ["TRANSFER", "CASH", "CHEQUE", "CARD", "EWALLET", "OTHER"] as const;
const REFUNDABLE = ["SCN", "SCA", "PV"];
let nextKey = 1;
const blank = (): Row => ({ key: nextKey++, description: "", ref_doc_no: "", tax_code_id: "", amount: "" });

export function NoteForm({ def, id }: { def: KindDef; id: string }) {
  const { t, can } = useSession();
  const nav = useNavigate();
  const refund = def.kind === "supplier-refund";
  const customer = def.party === "customer";
  const partyPath = customer ? "/customers" : "/suppliers";
  const listArea = def.area === "finance" ? "finance" : def.area === "sales" ? "sales" : "purchases";
  const listPath = `/${listArea}?kind=${def.kind}`;

  const [doc, setDoc] = useState<NoteDoc | null>(null);
  const [party, setParty] = useState<Party | null>(null);
  const [h, setHead] = useState({
    doc_date: today(), location_id: "", supplier_ref_no: "", remark: "", amount: "", method: "TRANSFER", bank_ref: "",
  });
  const [rows, setRows] = useState<Row[]>([blank()]);
  const [pick, setPick] = useState<Record<string, string>>({});  // refund: credit doc id -> amount
  const [credits, setCredits] = useState<OpenItem[]>([]);
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

  const editable = (!doc || doc.status === "DRAFT" || doc.status === "SAVED") && can(`${def.perm}.doc.create`);
  const setH = (patch: Partial<typeof h>) => { setHead((x) => ({ ...x, ...patch })); setDirty(true); };

  useEffect(() => {
    api<Page<TaxCode>>("/tax-codes").then((p) => setTaxes(p.items)).catch(() => undefined);
    api<Page<{ id: string; code: string; name: string }>>("/locations").then((p) => {
      setLocations(p.items);
      setHead((x) => (x.location_id || !p.items[0] ? x : { ...x, location_id: p.items[0].id }));
    }).catch(() => undefined);
  }, []);

  const apply = useCallback(async (d: NoteDoc) => {
    setDoc(d);
    const pid = (d.customer_id ?? d.supplier_id) as string;
    const sup = await api<Party>(`${partyPath}/${pid}`).catch(() => null);
    setParty(sup ?? { id: pid, code: "", name: "…" });
    setHead({
      doc_date: d.doc_date, location_id: d.location_id,
      supplier_ref_no: (customer ? d.customer_ref_no : d.supplier_ref_no) ?? "",
      remark: d.remark ?? "", amount: d.amount != null ? String(Number(d.amount)) : "",
      method: d.method ?? "TRANSFER", bank_ref: d.bank_ref ?? "",
    });
    if (refund) {
      setPick(Object.fromEntries(d.lines.map((l) => [l.doc_id as string, String(Number(l.amount))])));
    } else {
      setRows(d.lines.length ? d.lines.map((l) => ({
        key: nextKey++, description: l.description ?? "", ref_doc_no: l.ref_doc_no ?? "",
        tax_code_id: l.tax_code_id ?? "", amount: String(Number(l.amount)),
      })) : [blank()]);
    }
    setWarnings(d.warnings ?? []);
    setDirty(false);
  }, [refund, customer, partyPath]);

  useEffect(() => {
    if (id === "new") return;
    setLoading(true);
    api<NoteDoc>(`${def.api}/${id}`).then(apply).catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
  }, [id, def.api, apply]);

  // refund: the supplier's open credits it can settle
  useEffect(() => {
    if (!refund || !party) return;
    api<{ items: OpenItem[] }>("/finance/open-items", { query: { party_type: "supplier", party_id: party.id } })
      .then((r) => setCredits(r.items.filter((i) => i.side === "C" && REFUNDABLE.includes(i.doc_type))))
      .catch(() => setCredits([]));
  }, [refund, party, doc?.status]);

  const searchSuppliers = useCallback(
    (q: string) => api<Page<Party>>(partyPath, { query: { q, page_size: 8 } }).then((p) => p.items), [partyPath]);
  const rate = (tid: string) => Number(taxes.find((x) => x.id === tid)?.rate ?? 0);
  const est = useMemo(() => {
    let sub = 0, tax = 0;
    for (const r of rows) {
      if (!isNum(r.amount)) continue;
      const a = round2(Number(r.amount));
      sub += a;
      tax += round2((a * rate(r.tax_code_id)) / 100);
    }
    return { sub: round2(sub), tax: round2(tax), total: round2(sub + tax) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, taxes]);

  const check = (): string | null => {
    if (!party) return t("pickParty");
    if (refund) {
      if (!isNum(h.amount) || Number(h.amount) <= 0) return t("amountReceived");
      for (const [, v] of Object.entries(pick)) if (v.trim() && !isNum(v)) return t("amount");
      return null;
    }
    const used = rows.filter((r) => r.description.trim() || r.amount.trim());
    for (const r of used) {
      if (!r.description.trim()) return t("noteDescription");
      if (!isNum(r.amount) || Number(r.amount) <= 0) return t("amount");
    }
    return null;
  };
  const body = () => ({
    doc_date: h.doc_date, [`${def.party}_id`]: party?.id, location_id: h.location_id,
    [`${def.party}_ref_no`]: h.supplier_ref_no.trim() || null, remark: h.remark.trim() || null,
    ...(refund ? {
      amount: h.amount.trim(), method: h.method, bank_ref: h.bank_ref.trim() || null,
      lines: credits.concat((doc?.lines ?? []).filter((l) => !credits.some((c) => c.id === l.doc_id))
        .map((l) => ({ doc_type: l.doc_type as string, id: l.doc_id as string, doc_no: l.doc_no as string,
          doc_date: "", side: "C", open: "0" })))
        .filter((c) => isNum(pick[c.id] ?? "") && Number(pick[c.id]) > 0)
        .map((c) => ({ doc_type: c.doc_type, doc_id: c.id, amount: pick[c.id].trim() })),
    } : {
      lines: rows.filter((r) => r.description.trim() || r.amount.trim()).map((r) => ({
        description: r.description.trim(), ref_doc_no: r.ref_doc_no.trim() || null,
        tax_code_id: r.tax_code_id || null, amount: r.amount.trim(),
      })),
    }),
  });

  const persist = async (): Promise<NoteDoc> => {
    const bad = check();
    if (bad) throw new Error(bad);
    if (!doc) {
      const d = await api<NoteDoc>(def.api, { method: "POST", body: body() });
      await apply(d);
      nav(`/docs/${def.kind}/${d.id}`, { replace: true });
      return d;
    }
    if (!dirty) return doc;
    const d = await api<NoteDoc>(`${def.api}/${doc.id}`, { method: "PATCH", body: { version: doc.version, ...body() } });
    await apply(d);
    return d;
  };
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null); setNotice(null);
    try { await fn(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const step = (path: string, extra: Record<string, unknown> = {}) => run(async () => {
    const d = await persist();
    await apply(await api<NoteDoc>(`${def.api}/${d.id}/${path}`, { method: "POST", body: { version: d.version, ...extra } }));
    setNotice(t("saved"));
  });
  const remove = () => run(async () => {
    if (!doc || !window.confirm(t("confirmDelete"))) return;
    await api(`${def.api}/${doc.id}`, { method: "DELETE" });
    nav(listPath);
  });

  if (loading) return <section className="card glass"><span className="muted">{t("loading")}</span></section>;
  if (loadError || (!doc && !can(`${def.perm}.doc.create`))) {
    return (
      <section className="card glass">
        <Link to={listPath} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
        <div className="error" role="alert">{loadError ?? t("noAccess")}</div>
      </section>
    );
  }
  const status = doc?.status ?? "NEW";
  const setRow = (key: number, patch: Partial<Row>) => {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
    setDirty(true);
  };
  const shownCredits = refund
    ? credits.concat((doc?.lines ?? []).filter((l) => !credits.some((c) => c.id === l.doc_id)).map((l) => ({
      doc_type: l.doc_type as string, id: l.doc_id as string, doc_no: l.doc_no as string, doc_date: "", side: "C", open: "0",
    })))
    : [];
  const named = Object.values(pick).reduce((s, v) => s + (isNum(v) ? Number(v) : 0), 0);

  return (
    <>
    <div className="docform no-print">
      <section className="card glass">
        <div className="row">
          <div>
            <Link to={listPath} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
            <h2 style={{ margin: "4px 0 0" }}>{t(def.kind)} {doc?.doc_no ?? (doc ? `(${t("draft")})` : "")}</h2>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {doc && <button className="chip-btn" disabled={dirty} onClick={() => window.print()}>{t("printDoc")}</button>}
            <span className={`badge ${STATUS_COLOR[status] ?? ""}`}>{status}</span>
          </div>
        </div>
        <span className="muted">{t(`hint_${def.kind}` as Key)}</span>
        <div className="doc-head">
          <div className="field wide">
            <label htmlFor="party">{t(customer ? "customer" : "supplier")}</label>
            <Picker<Party> id="party" search={searchSuppliers} keepText disabled={!editable}
                    initial={party ? [party.code, party.name].filter(Boolean).join(" · ") : ""}
                    label={(p) => `${p.code} · ${p.name}`} placeholder={t("searchParty")}
                    onPick={(p) => { setParty(p); setPick({}); setDirty(true); }} autoFocus={!doc} />
          </div>
          <div className="field">
            <label htmlFor="date">{t("date")}</label>
            <input id="date" className="input" type="date" value={h.doc_date} disabled={!editable}
                   onChange={(e) => setH({ doc_date: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="store">{t("store")}</label>
            <select id="store" className="input" value={h.location_id} disabled={!editable}
                    onChange={(e) => setH({ location_id: e.target.value })}>
              {locations.map((l) => <option key={l.id} value={l.id}>{l.code} · {l.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="ref">{t(customer ? "customerDocNo" : "supplierDocNo")}</label>
            <input id="ref" className="input" value={h.supplier_ref_no} disabled={!editable} maxLength={50}
                   onChange={(e) => setH({ supplier_ref_no: e.target.value })} />
          </div>
          {refund && (
            <>
              <div className="field">
                <label htmlFor="amt">{t("amountReceived")}</label>
                <input id="amt" className="input num" inputMode="decimal" value={h.amount} disabled={!editable}
                       onChange={(e) => setH({ amount: e.target.value })} />
              </div>
              <div className="field">
                <label htmlFor="method">{t("method")}</label>
                <select id="method" className="input" value={h.method} disabled={!editable}
                        onChange={(e) => setH({ method: e.target.value })}>
                  {METHODS.map((m) => <option key={m} value={m}>{t(`method${m}` as Key)}</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor="bref">{t("bankRef")}</label>
                <input id="bref" className="input" value={h.bank_ref} disabled={!editable} maxLength={50}
                       onChange={(e) => setH({ bank_ref: e.target.value })} />
              </div>
            </>
          )}
          <div className="field wide">
            <label htmlFor="remark">{t("remark")}</label>
            <input id="remark" className="input" value={h.remark} disabled={!editable} maxLength={500}
                   onChange={(e) => setH({ remark: e.target.value })} />
          </div>
        </div>
      </section>

      <section className="card glass">
        {refund ? (
          <>
            <h3>{t("refundCredits")}</h3>
            <span className="muted">{t("refundCreditsHint")}</span>
            {!party ? <span className="muted">{t("pickParty")}</span> : shownCredits.length === 0
              ? <span className="muted">{t("noOpenCredits")}</span> : (
                <div className="scroll-x">
                  <table className="table doc-lines">
                    <thead><tr><th>{t("docNo")}</th><th>{t("date")}</th><th className="num">{t("stillOpen")}</th>
                      <th className="num">{t("refundNow")}</th></tr></thead>
                    <tbody>
                      {shownCredits.map((c) => (
                        <tr key={c.id}>
                          <td>{c.doc_no} <span className="muted">{c.doc_type}</span></td>
                          <td>{c.doc_date}</td>
                          <td className="num">{money(c.open)}</td>
                          <td className="num">
                            <input className="input sm num" inputMode="decimal" value={pick[c.id] ?? ""} disabled={!editable}
                                   aria-label={`${t("refundNow")} ${c.doc_no}`}
                                   onChange={(e) => { setPick({ ...pick, [c.id]: e.target.value }); setDirty(true); }} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            <div className="totals">
              <div><span>{t("amountReceived")}</span><b>{money(isNum(h.amount) ? Number(h.amount) : 0)}</b></div>
              <div><span>{t("assigned")}</span><b>{money(named)}</b></div>
              {doc?.open != null && <div><span>{t("stillOpen")}</span><b>{money(doc.open)}</b></div>}
            </div>
          </>
        ) : (
          <>
            <h3>{t("lines")}</h3>
            <div className="scroll-x">
              <table className="table doc-lines">
                <thead><tr><th>#</th><th>{t("noteDescription")}</th><th>{t("forInvoice")}</th><th>{t("tax")}</th>
                  <th className="num">{t("amountExTax")}</th>{editable && <th />}</tr></thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={r.key}>
                      <td className="muted">{i + 1}</td>
                      <td><input className="input sm" style={{ minWidth: 220 }} value={r.description} disabled={!editable}
                                 maxLength={200} aria-label={t("noteDescription")}
                                 onChange={(e) => setRow(r.key, { description: e.target.value })} /></td>
                      <td><input className="input sm" value={r.ref_doc_no} disabled={!editable} maxLength={50}
                                 aria-label={t("forInvoice")} onChange={(e) => setRow(r.key, { ref_doc_no: e.target.value })} /></td>
                      <td>
                        <select className="input sm" value={r.tax_code_id} disabled={!editable} aria-label={t("tax")}
                                onChange={(e) => setRow(r.key, { tax_code_id: e.target.value })}>
                          <option value="">{t("noTax")}</option>
                          {taxes.filter((x) => x.is_active || x.id === r.tax_code_id).map((x) => (
                            <option key={x.id} value={x.id}>{x.code} {Number(x.rate)}%</option>
                          ))}
                        </select>
                      </td>
                      <td className="num"><input className="input sm num" inputMode="decimal" value={r.amount} disabled={!editable}
                                 aria-label={t("amountExTax")} onChange={(e) => setRow(r.key, { amount: e.target.value })} /></td>
                      {editable && (
                        <td><button className="chip-btn" aria-label={t("remove")}
                                    onClick={() => { setRows((rs) => rs.filter((x) => x.key !== r.key)); setDirty(true); }}>✕</button></td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {editable && (
              <button className="chip-btn" style={{ alignSelf: "flex-start" }}
                      onClick={() => setRows((rs) => [...rs, blank()])}>+ {t("addLine")}</button>
            )}
            <div className="totals">
              <div><span>{t("subtotalLabel")}</span><b>{money(doc && !dirty ? doc.subtotal : est.sub)}</b></div>
              <div><span>{t("taxLabel")}</span><b>{money(doc && !dirty ? doc.tax_total : est.tax)}</b></div>
              <div className="grand"><span>{t("total")}</span><b>{money(doc && !dirty ? doc.total : est.total)}</b></div>
              {doc?.open != null && <div><span>{t("stillOpen")}</span><b>{money(doc.open)}</b></div>}
            </div>
          </>
        )}

        <div className="actions">
          {status !== "POSTED" && status !== "VOIDED" && editable && (status === "NEW" || status === "DRAFT") && (
            <button className="btn ghost" disabled={busy} onClick={() => run(async () => { await persist(); setNotice(t("saved")); })}>
              {t("saveDraft")}
            </button>
          )}
          {(status === "NEW" || status === "DRAFT") && editable && (
            <button className="btn" disabled={busy} onClick={() => step("save")}>{t("saveDoc")}</button>
          )}
          {status === "SAVED" && editable && (
            <button className="btn ghost" disabled={busy || !dirty}
                    onClick={() => run(async () => { await persist(); setNotice(t("saved")); })}>{t("update")}</button>
          )}
          {status === "SAVED" && can(`${def.perm}.doc.post`) && (
            <button className="btn green" disabled={busy} onClick={() => step("post")}>{t("post")}</button>
          )}
          {doc && (status === "SAVED" || status === "POSTED") && can(`${def.perm}.doc.void`) && (
            <button className="btn ghost danger" disabled={busy} onClick={() => setVoiding((v) => !v)}>{t("voidDoc")}</button>
          )}
          {status === "DRAFT" && editable && (
            <button className="btn ghost danger" disabled={busy} onClick={remove}>{t("deleteDraft")}</button>
          )}
        </div>
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
          <div className="warn-box">{warnings.map((w, i) => <div key={i}>{warnText(t, w)}</div>)}</div>
        )}
        {doc?.void_reason && <div className="muted">{t("voidReason")}: {doc.void_reason}</div>}
      </section>
    </div>
    {doc && (
      <PrintSheet
        title={t(def.kind).toUpperCase()}
        docNo={doc.doc_no}
        status={doc.status}
        partyLabel={t(customer ? "billTo" : "supplier")}
        party={party}
        meta={[
          [t("date"), doc.doc_date],
          [t(customer ? "customerDocNo" : "supplierDocNo"), (customer ? doc.customer_ref_no : doc.supplier_ref_no) ?? null],
          ...(customer ? [[t("dueDate"), (doc as NoteDoc & { due_date?: string }).due_date ?? null]] as [string, string | null][] : []),
          ...(refund ? [[t("method"), t(`method${doc.method}` as Key)], [t("bankRef"), doc.bank_ref]] as [string, string | null][] : []),
        ]}
        columns={refund
          ? [{ label: "#", width: "28px" }, { label: t("docNo") }, { label: t("amountRM"), num: true }]
          : [{ label: "#", width: "28px" }, { label: t("noteDescription") }, { label: t("forInvoice") },
             { label: t("amountRM"), num: true }]}
        rows={doc.lines.map((l, i) => refund
          ? [i + 1, l.doc_no, amt(l.amount)]
          : [i + 1, l.description, l.ref_doc_no, amt(l.amount)])}
        totals={refund
          ? [[t("amountReceived"), money(doc.amount), true]]
          : [[t("subtotalLabel"), money(doc.subtotal)], [t("taxLabel"), money(doc.tax_total)],
             [t("total"), money(doc.total), true]]}
        remark={doc.remark}
        signatures={[t("sigPrepared"), t("sigAuthorised")]}
      />
    )}
    </>
  );
}
