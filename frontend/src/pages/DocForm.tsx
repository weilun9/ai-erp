import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, money, qty as fmtQty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { KINDS, STATUS_COLOR, isKind } from "../docs/kinds";
import type { Kind, KindDef } from "../docs/kinds";
import { amt, errorText, isNum, round2, today, warnText } from "../docs/shared";
import { PrintSheet } from "../docs/PrintSheet";
import type { PrintParty } from "../docs/PrintSheet";
import type { Warning } from "../docs/shared";
import { PayForm } from "./PayForm";

// One entry screen for quotations, DOs, sales invoices, customer CNs, GRNs and supplier CNs.
// The server owns every number that matters (price lookup, tax, totals, credit and cost
// checks); this page only collects input and shows an estimate until the document is saved.

interface ItemUom { uom: string; factor: string; selling_price: string | null; purchase_price?: string | null }
interface Item {
  id: string; code: string; name: string; base_uom: string; uoms: ItemUom[];
  sales_tax_code_id: string | null; purchase_tax_code_id: string | null;
}
interface TaxCode { id: string; code: string; name: string; rate: string; is_active: boolean }
interface Party extends PrintParty { id: string; code: string; name: string }
interface LineOut {
  id: string; item_id: string; uom: string; qty: string; unit_price?: string; price_source: string;
  discount_pct: string; tax_code_id: string | null; line_total?: string; remark: string | null;
  tax_rate?: string;
  do_line_id?: string | null;
}
interface DocOut {
  id: string; doc_no: string | null; doc_date: string; status: string; version: number;
  customer_id?: string; supplier_id?: string; location_id: string;
  customer_ref_no?: string | null; supplier_ref_no?: string | null; remark: string | null;
  subtotal?: string | null; tax_total?: string | null; total?: string | null;
  gross_profit?: string | null; due_date?: string | null; valid_until?: string | null;
  ref_invoice_id?: string | null; reason?: string | null;
  completeness?: string | null; ref_invoice_no?: string | null;
  source_kind?: string | null; void_reason: string | null;
  lines: LineOut[]; warnings?: Warning[];
}
interface Row {
  key: number; item: Item; uom: string; qty: string; unit_price: string; discount_pct: string;
  tax_code_id: string; remark: string; do_line_id: string | null;
  serverPrice?: string;  // what the server priced a line left on "auto"
}
interface Head {
  doc_date: string; party: Party | null; location_id: string; ref_no: string; remark: string;
  valid_until: string; ref_invoice_id: string; reason: string; completeness: string;
  ref_invoice_no: string;
}

let nextKey = 1;

const emptyHead = (): Head => ({
  doc_date: today(), party: null, location_id: "", ref_no: "", remark: "", valid_until: "",
  ref_invoice_id: "", reason: "", completeness: "", ref_invoice_no: "",
});

function newRow(def: KindDef, item: Item, uom?: string): Row {
  const u = uom ?? item.base_uom;
  const cost = item.uoms.find((x) => x.uom === u)?.purchase_price;
  return {
    key: nextKey++, item, uom: u, qty: "1",
    // a GRN needs the supplier's price: start from the last known one
    unit_price: def.priceRequired && cost ? String(Number(cost)) : "",
    discount_pct: "0", tax_code_id: item[def.taxField] ?? "", remark: "", do_line_id: null,
  };
}

export function DocForm() {
  const { kind: kindParam, id } = useParams();
  if (!isKind(kindParam)) return <section className="card glass">{"Unknown document type"}</section>;
  // keyed by kind only: saving a new document swaps "new" for its id without a remount
  if (KINDS[kindParam].area === "finance") {
    return <PayForm key={kindParam} def={KINDS[kindParam]} id={id ?? "new"} />;
  }
  return <Form key={kindParam} def={KINDS[kindParam]} id={id ?? "new"} />;
}

function Form({ def, id }: { def: KindDef; id: string }) {
  const { t, can } = useSession();
  const nav = useNavigate();
  const [doc, setDoc] = useState<DocOut | null>(null);
  const [head, setHead] = useState<Head>(emptyHead);
  const [rows, setRows] = useState<Row[]>([]);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(id !== "new");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [taxes, setTaxes] = useState<TaxCode[]>([]);
  const [locations, setLocations] = useState<{ id: string; code: string; name: string }[]>([]);
  const [invoices, setInvoices] = useState<{ id: string; doc_no: string; doc_date: string; total: string }[]>([]);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);
  const items = useRef(new Map<string, Item>());
  const listPath = `/${def.area === "sales" ? "sales" : "purchases"}?kind=${def.kind}`;
  const partyPath = def.party === "customer" ? "/customers" : "/suppliers";

  const editable = (!doc || doc.status === "DRAFT" || doc.status === "SAVED")
    && can(`${def.perm}.doc.create`);

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

  const apply = useCallback(async (d: DocOut, keepWarnings = false) => {
    const missing = [...new Set(d.lines.map((l) => l.item_id))].filter((i) => !items.current.has(i));
    const fetched = await Promise.all(missing.map((i) => api<Item>(`/items/${i}`)));
    fetched.forEach((it) => items.current.set(it.id, it));
    const partyId = (d.customer_id ?? d.supplier_id) as string;
    const party = await api<Party>(`${partyPath}/${partyId}`).catch(() => null);
    setDoc(d);
    setHead({
      doc_date: d.doc_date, party: party ?? { id: partyId, code: "", name: "…" },
      location_id: d.location_id, ref_no: (d[def.refField] as string | null) ?? "",
      remark: d.remark ?? "", valid_until: d.valid_until ?? "",
      ref_invoice_id: d.ref_invoice_id ?? "", reason: d.reason ?? "",
      completeness: d.completeness ?? "", ref_invoice_no: d.ref_invoice_no ?? "",
    });
    setRows(d.lines.map((l) => {
      const own = l.price_source === "DOC";
      return {
        key: nextKey++, item: items.current.get(l.item_id) as Item, uom: l.uom,
        qty: String(Number(l.qty)),
        unit_price: own && l.unit_price != null ? String(Number(l.unit_price)) : "",
        serverPrice: own ? undefined : l.unit_price,
        discount_pct: String(Number(l.discount_pct)), tax_code_id: l.tax_code_id ?? "",
        remark: l.remark ?? "", do_line_id: l.do_line_id ?? null,
      };
    }));
    if (!keepWarnings) setWarnings(d.warnings ?? []);
    setDirty(false);
  }, [def.refField, partyPath]);

  useEffect(() => {
    if (id === "new") {
      if (doc) {  // "+ New" while another document of this kind is open
        setDoc(null);
        setHead((h) => ({ ...emptyHead(), location_id: h.location_id }));
        setRows([]);
        setWarnings([]);
        setError(null);
        setNotice(null);
        setDirty(false);
      }
      return;
    }
    if (doc?.id === id) return;  // just created here: already on screen
    setLoading(true);
    api<DocOut>(`${def.api}/${id}`)
      .then((d) => apply(d))
      .catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // a customer return points at one of that customer's posted invoices
  useEffect(() => {
    if (def.kind !== "customer-cn" || !head.party) return setInvoices([]);
    api<Page<{ id: string; doc_no: string; doc_date: string; total: string }>>(
      "/sales/sales-invoice", { query: { customer_id: head.party.id, status: "POSTED", page_size: 100 } },
    ).then((p) => setInvoices(p.items)).catch(() => setInvoices([]));
  }, [def.kind, head.party]);

  // --- editing -----------------------------------------------------------------------------

  const setH = (patch: Partial<Head>) => { setHead((h) => ({ ...h, ...patch })); setDirty(true); };
  const setRow = (key: number, patch: Partial<Row>) => {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
    setDirty(true);
  };
  const addItem = (item: Item, uom?: string) => {
    items.current.set(item.id, item);
    setRows((rs) => [...rs, newRow(def, item, uom)]);
    setDirty(true);
    setNotice(null);
  };

  const searchItems = useCallback(
    (q: string) => api<Page<Item>>("/items", { query: { q, page_size: 8 } }).then((p) => p.items), []);
  const searchParties = useCallback(
    (q: string) => api<Page<Party>>(partyPath, { query: { q, page_size: 8 } }).then((p) => p.items),
    [partyPath]);
  const scan = async (text: string) => {
    if (!/^\d{6,}$/.test(text)) return false;
    const r = await api<{ hits: { matched_uom: string; item: Item }[] }>("/items/lookup",
      { query: { barcode: text } }).catch(() => ({ hits: [] }));
    const ids = new Set(r.hits.map((h) => h.item.id));
    if (ids.size === 1) {
      addItem(r.hits[0].item, r.hits[0].matched_uom);
      return true;
    }
    setNotice(`${t("notFound")}: ${text}`);
    return false;
  };

  // --- numbers shown before the server has priced the document -----------------------------

  const rate = (taxId: string) => Number(taxes.find((x) => x.id === taxId)?.rate ?? 0);
  const priceOf = (r: Row): number | null => {
    if (isNum(r.unit_price)) return Number(r.unit_price);
    if (r.serverPrice != null) return Number(r.serverPrice);
    const list = r.item.uoms.find((u) => u.uom === r.uom)?.[def.priceField];
    return list != null ? Number(list) : null;
  };
  const amounts = (r: Row) => {
    const p = priceOf(r);
    if (p === null || !isNum(r.qty)) return null;
    const sub = round2(Number(r.qty) * p * (1 - Number(r.discount_pct || 0) / 100));
    return { sub, tax: round2((sub * rate(r.tax_code_id)) / 100) };
  };
  const estimate = useMemo(() => {
    let sub = 0, tax = 0;
    for (const r of rows) {
      const a = amounts(r);
      if (!a) return null;
      sub += a.sub;
      tax += a.tax;
    }
    return { sub: round2(sub), tax: round2(tax), total: round2(sub + tax) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, taxes]);
  const showServer = doc && !dirty;

  // --- talking to the server -----------------------------------------------------------------

  const at = (i: number) => t("lineNo").replace("{n}", String(i + 1));
  const check = (): string | null => {
    if (!head.party) return t("pickParty");
    if (!head.location_id) return t("store");
    for (const [i, r] of rows.entries()) {
      if (!isNum(r.qty) || Number(r.qty) <= 0) return `${at(i)}: ${t("qtyLabel")}`;
      if (r.unit_price !== "" && !isNum(r.unit_price)) return `${at(i)}: ${t("unitPrice")}`;
      if (def.priceRequired && r.unit_price === "") return `${at(i)}: ${t("priceRequired")}`;
    }
    return null;
  };

  const body = () => ({
    doc_date: head.doc_date,
    [`${def.party}_id`]: head.party?.id,
    location_id: head.location_id,
    [def.refField]: head.ref_no.trim() || null,
    remark: head.remark.trim() || null,
    ...(def.kind === "quotation" ? { valid_until: head.valid_until || null } : {}),
    ...(def.kind === "customer-cn"
      ? { ref_invoice_id: head.ref_invoice_id || null, reason: head.reason.trim() || null } : {}),
    ...(def.kind === "supplier-cn"
      ? { completeness: head.completeness || undefined, ref_invoice_no: head.ref_invoice_no.trim() || null }
      : {}),
    lines: rows.map((r) => ({
      item_id: r.item.id, uom: r.uom, qty: r.qty.trim(),
      unit_price: r.unit_price.trim() === "" ? undefined : r.unit_price.trim(),  // left out = auto
      discount_pct: r.discount_pct.trim() || "0",
      tax_code_id: r.tax_code_id || null,
      remark: r.remark.trim() || null,
      ...(r.do_line_id ? { do_line_id: r.do_line_id } : {}),
    })),
  });

  /** Writes unsaved edits (creating the draft the first time) and returns the current doc. */
  const persist = async (): Promise<DocOut> => {
    const bad = check();
    if (bad) throw new Error(bad);
    if (!doc) {
      const d = await api<DocOut>(def.api, { method: "POST", body: body() });
      await apply(d);
      nav(`/docs/${def.kind}/${d.id}`, { replace: true });
      return d;
    }
    if (!dirty) return doc;
    const d = await api<DocOut>(`${def.api}/${doc.id}`,
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
    const after = await api<DocOut>(`${def.api}/${d.id}/${path}`,
      { method: "POST", body: { version: d.version, ...extra } });
    await apply(after);
    setNotice(t("saved"));
  });

  const saveDraft = () => run(async () => { await persist(); setNotice(t("saved")); });
  const remove = () => run(async () => {
    if (!doc || !window.confirm(t("confirmDelete"))) return;
    await api(`${def.api}/${doc.id}`, { method: "DELETE" });
    nav(listPath);
  });
  const convert = (target: Kind) => run(async () => {
    const d = await persist();
    const made = await api<DocOut>(`/sales/${target}/convert`,
      { method: "POST", body: { source_kind: def.kind, source_id: d.id } });
    nav(`/docs/${target}/${made.id}`);
  });

  // --- screen -------------------------------------------------------------------------------

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
  const priceHidden = doc?.lines.some((l) => l.unit_price === undefined);
  const uomsOf = (r: Row) => r.item.uoms.map((u) => u.uom);

  // --- the paper version (from the saved document) ---------------------------------------------
  const isDO = def.kind === "delivery-order";  // a delivery order carries no prices
  const store = locations.find((l) => l.id === doc?.location_id);
  const printRows = (doc?.lines ?? []).map((l, i) => {
    const it = items.current.get(l.item_id);
    const base = [i + 1, `${it?.code ?? ""}  ${it?.name ?? ""}`, fmtQty(l.qty), l.uom];
    return isDO ? base : [
      ...base,
      l.unit_price != null ? amt(l.unit_price) : "",
      Number(l.discount_pct) ? `${fmtQty(l.discount_pct)}%` : "",
      Number(l.tax_rate ?? 0) ? `${fmtQty(l.tax_rate as string)}%` : "",
      l.line_total != null ? amt(l.line_total) : "",
    ];
  });
  const printColumns = [
    { label: "#", width: "28px" }, { label: t("item") }, { label: t("qtyLabel"), num: true }, { label: t("uom") },
    ...(isDO ? [] : [
      { label: t("unitPrice"), num: true }, { label: t("discPct"), num: true },
      { label: t("tax"), num: true }, { label: t("amountRM"), num: true },
    ]),
  ];

  return (
    <>
    <div className="docform no-print">
      <section className="card glass">
        <div className="row">
          <div>
            <Link to={listPath} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
            <h2 style={{ margin: "4px 0 0" }}>
              {t(def.kind)} {doc?.doc_no ?? <span className="muted">{t("draftNo")}</span>}
            </h2>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {doc && (
              <button className="chip-btn" disabled={dirty} title={dirty ? t("printSaveFirst") : undefined}
                      onClick={() => window.print()}>{t("printDoc")}</button>
            )}
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
                    onPick={(p) => setH({ party: p, ref_invoice_id: "" })} autoFocus={!doc} />
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
            <label htmlFor="ref">{t(def.party === "customer" ? "customerRef" : "supplierRef")}</label>
            <input id="ref" className="input" value={head.ref_no} disabled={!editable} maxLength={50}
                   onChange={(e) => setH({ ref_no: e.target.value })} />
          </div>
          {def.kind === "quotation" && (
            <div className="field">
              <label htmlFor="valid">{t("validUntil")}</label>
              <input id="valid" className="input" type="date" value={head.valid_until} disabled={!editable}
                     onChange={(e) => setH({ valid_until: e.target.value })} />
            </div>
          )}
          {def.kind === "customer-cn" && (
            <>
              <div className="field">
                <label htmlFor="refinv">{t("refInvoice")}</label>
                <select id="refinv" className="input" value={head.ref_invoice_id} disabled={!editable}
                        onChange={(e) => setH({ ref_invoice_id: e.target.value })}>
                  <option value="">—</option>
                  {invoices.map((i) => (
                    <option key={i.id} value={i.id}>{i.doc_no} · {i.doc_date} · {money(i.total)}</option>
                  ))}
                  {head.ref_invoice_id && !invoices.some((i) => i.id === head.ref_invoice_id) && (
                    <option value={head.ref_invoice_id}>…</option>
                  )}
                </select>
              </div>
              <div className="field">
                <label htmlFor="reason">{t("reason")}</label>
                <input id="reason" className="input" value={head.reason} disabled={!editable} maxLength={200}
                       onChange={(e) => setH({ reason: e.target.value })} />
              </div>
            </>
          )}
          {def.kind === "supplier-cn" && (
            <>
              <div className="field">
                <label htmlFor="refno">{t("refInvoiceNo")}</label>
                <input id="refno" className="input" value={head.ref_invoice_no} disabled={!editable}
                       maxLength={50} onChange={(e) => setH({ ref_invoice_no: e.target.value })} />
              </div>
              <div className="field">
                <label htmlFor="comp">{t("completeness")}</label>
                <select id="comp" className="input" value={head.completeness} disabled={!editable}
                        onChange={(e) => setH({ completeness: e.target.value })}>
                  <option value="">—</option>
                  <option value="RECORDED">{t("compRECORDED")}</option>
                  <option value="PROVISIONAL">{t("compPROVISIONAL")}</option>
                  <option value="COMPLETE">{t("compCOMPLETE")}</option>
                  {head.completeness === "DISPUTED" && <option value="DISPUTED">{t("compDISPUTED")}</option>}
                </select>
              </div>
            </>
          )}
          <div className="field wide">
            <label htmlFor="remark">{t("remark")}</label>
            <input id="remark" className="input" value={head.remark} disabled={!editable} maxLength={500}
                   onChange={(e) => setH({ remark: e.target.value })} />
          </div>
        </div>
        {doc?.due_date && <span className="muted">{t("dueDate")}: {doc.due_date}</span>}
        {doc?.void_reason && <span className="error">{t("voidDoc")}: {doc.void_reason}</span>}
      </section>

      <section className="card glass">
        <h3>{t("lines")}</h3>
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead>
              <tr>
                <th>#</th><th>{t("item")}</th><th>{t("uom")}</th>
                <th className="num">{t("qtyLabel")}</th><th className="num">{t("unitPrice")}</th>
                <th className="num">{t("discPct")}</th><th>{t("tax")}</th>
                <th className="num">{t("lineTotal")}</th>{editable && <th />}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={9} className="muted">{t("noLines")}</td></tr>
              )}
              {rows.map((r, i) => {
                const a = amounts(r);
                const auto = priceOf(r);
                return (
                  <tr key={r.key}>
                    <td className="muted">{i + 1}</td>
                    <td>
                      <div>{r.item.name}</div>
                      <span className="muted">{r.item.code}{r.do_line_id ? ` · ${t("fromDo")}` : ""}</span>
                    </td>
                    <td>
                      <select className="input sm" value={r.uom} disabled={!editable || !!r.do_line_id}
                              aria-label={t("uom")}
                              onChange={(e) => setRow(r.key, { uom: e.target.value, serverPrice: undefined })}>
                        {uomsOf(r).map((u) => <option key={u}>{u}</option>)}
                      </select>
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={r.qty} disabled={!editable}
                             aria-label={t("qtyLabel")}
                             onChange={(e) => setRow(r.key, { qty: e.target.value })} />
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={r.unit_price}
                             disabled={!editable} aria-label={t("unitPrice")}
                             placeholder={auto !== null && r.unit_price === ""
                               ? `${t("auto")} ${auto.toFixed(2)}` : def.priceRequired ? "" : t("auto")}
                             onChange={(e) => setRow(r.key, { unit_price: e.target.value })} />
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={r.discount_pct}
                             disabled={!editable} aria-label={t("discPct")}
                             onChange={(e) => setRow(r.key, { discount_pct: e.target.value })} />
                    </td>
                    <td>
                      <select className="input sm" value={r.tax_code_id} disabled={!editable}
                              aria-label={t("tax")}
                              onChange={(e) => setRow(r.key, { tax_code_id: e.target.value })}>
                        <option value="">{t("noTax")}</option>
                        {taxes.filter((x) => x.is_active || x.id === r.tax_code_id).map((x) => (
                          <option key={x.id} value={x.id}>{x.code} {fmtQty(x.rate)}%</option>
                        ))}
                      </select>
                    </td>
                    <td className="num">{a ? money(a.sub + a.tax) : "—"}</td>
                    {editable && (
                      <td>
                        <button className="chip-btn" aria-label={t("remove")}
                                onClick={() => { setRows((rs) => rs.filter((x) => x.key !== r.key)); setDirty(true); }}>
                          ✕
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {editable && (
          <Picker<Item> search={searchItems} onPick={(it) => addItem(it)} onEnter={scan}
                  label={(it) => `${it.code} · ${it.name}`} placeholder={t("addItem")} />
        )}

        <div className="doc-foot">
          <div className="totals">
            {showServer ? (
              <>
                <div><span>{t("subtotalLabel")}</span><b>{money(doc.subtotal)}</b></div>
                <div><span>{t("taxLabel")}</span><b>{money(doc.tax_total)}</b></div>
                <div className="grand"><span>{t("total")}</span><b>{money(doc.total)}</b></div>
                {doc.gross_profit != null && (
                  <div><span>{t("grossProfit")}</span><b>{money(doc.gross_profit)}</b></div>
                )}
              </>
            ) : (
              <>
                <div><span>{t("subtotalLabel")}</span><b>{money(estimate?.sub)}</b></div>
                <div><span>{t("taxLabel")}</span><b>{money(estimate?.tax)}</b></div>
                <div className="grand"><span>{t("total")}</span><b>{money(estimate?.total)}</b></div>
                <span className="muted">{t("estimated")}</span>
              </>
            )}
            {priceHidden && <span className="muted">{t("priceHidden")}</span>}
          </div>

          <div className="actions">
            {editable && status !== "SAVED" && (
              <button className="btn ghost" disabled={busy} onClick={saveDraft}>{t("saveDraft")}</button>
            )}
            {editable && status !== "SAVED" && (
              <button className="btn" disabled={busy} onClick={() => step("save")}>{t("saveDoc")}</button>
            )}
            {editable && status === "SAVED" && (
              <button className={def.posts ? "btn ghost" : "btn"} disabled={busy || !dirty}
                      onClick={saveDraft}>{t("update")}</button>
            )}
            {status === "SAVED" && def.posts && can(`${def.perm}.doc.post`) && (
              <button className="btn green" disabled={busy} onClick={() => step("post")}>{t("post")}</button>
            )}
            {doc && def.converts.filter((c) => c.from === status).map((c) =>
              can(`${def.perm}.doc.create`) && (
                <button key={c.target} className="btn ghost" disabled={busy}
                        onClick={() => convert(c.target)}>{t("convertTo")} {t(c.target).toLowerCase()}</button>
              ))}
            {doc && (status === "SAVED" || status === "POSTED") && can(`${def.perm}.doc.void`) && (
              <button className="btn ghost danger" disabled={busy}
                      onClick={() => setVoiding((v) => !v)}>{t("voidDoc")}</button>
            )}
            {status === "DRAFT" && editable && (
              <button className="btn ghost danger" disabled={busy} onClick={remove}>{t("deleteDraft")}</button>
            )}
          </div>
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
          <div className="warn-box">
            <b>{t("warnings")}</b>
            <ul>{warnings.map((w, i) => <li key={i}>{warnText(t, w)}</li>)}</ul>
          </div>
        )}
      </section>
    </div>
    {doc && (
      <PrintSheet
        title={t(def.kind).toUpperCase()}
        docNo={doc.doc_no}
        status={doc.status}
        partyLabel={t(def.party === "supplier" ? "supplier" : isDO ? "deliverTo" : "billTo")}
        party={head.party}
        meta={[
          [t("date"), doc.doc_date],
          [t("store"), store ? `${store.code} · ${store.name}` : null],
          [t(def.party === "customer" ? "customerRef" : "supplierRef"), doc[def.refField] as string | null],
          [t("dueDate"), doc.due_date],
          [t("validUntil"), doc.valid_until],
          [t("refInvoice"), invoices.find((x) => x.id === doc.ref_invoice_id)?.doc_no],
          [t("refInvoiceNo"), doc.ref_invoice_no],
          [t("reason"), doc.reason],
        ]}
        columns={printColumns}
        rows={printRows}
        totals={isDO ? [] : [
          [t("subtotalLabel"), money(doc.subtotal)],
          [t("taxLabel"), money(doc.tax_total)],
          [t("total"), money(doc.total), true],
        ]}
        remark={doc.remark}
        signatures={isDO ? [t("sigIssued"), t("sigReceived")]
          : def.kind === "grn" ? [t("sigReceivedBy"), t("sigChecked")] : [t("sigAuthorised")]}
      />
    )}
    </>
  );
}
