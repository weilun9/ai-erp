import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { api } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { errorText } from "../docs/shared";
import type { Key } from "../i18n";

// The spreadsheet pop-up of the scan screen: the recognised paper as editable cells - the
// header, then its lines - each cell marked by the server's checks (fine / check / missing).
// Top right: Edit (switch cells on), Undo (every change is a step; Ctrl+Z too), Cancel and
// Confirm. Confirm turns it into a DRAFT document; the scan screen then opens the next paper.

export type Kind = "INVOICE" | "CN" | "PAYMENT" | "BANK" | "RETURN_NOTE" | "OTHER";
export type SheetData = Record<string, unknown> & { kind?: Kind };
export interface Review {
  kind: Kind; data: SheetData; fields: Record<string, string>;
  findings: { code: string; message: string; line?: number; candidates?: { id: string; code?: string; name: string }[] }[];
  ready: boolean;
}
type Line = Record<string, string | null>;
interface Col { key: string; label: Key; type?: "num" | "date" | "select" | "item"; options?: string[]; width?: number }

const ITEM_COLS: Col[] = [
  { key: "item", label: "item", type: "item", width: 220 },
  { key: "description", label: "scanPrinted", width: 200 },
  { key: "barcode", label: "barcode", width: 130 },
  { key: "uom", label: "uom", width: 70 },
  { key: "qty", label: "qtyLabel", type: "num", width: 80 },
  { key: "unit_price", label: "unitPrice", type: "num", width: 100 },
  { key: "discount_pct", label: "discPct", type: "num", width: 70 },
  { key: "amount", label: "amount", type: "num", width: 100 },
];
const PAY_COLS: Col[] = [
  { key: "line_type", label: "lineType", type: "select", options: ["INVOICE", "CN", "FEE", "OTHER"], width: 110 },
  { key: "ref_no", label: "supplierDocNo", width: 160 },
  { key: "amount", label: "amount", type: "num", width: 110 },
  { key: "fee_type", label: "scanFeeType", type: "select", options: ["", "FREIGHT", "LISTING", "PROMOTION", "DISPLAY", "REBATE", "OTHER"], width: 120 },
  { key: "remark", label: "remark", width: 200 },
];
const FLAG: Record<string, string> = { TRUSTED: "ok", CHECK: "check", MISSING: "missing" };
const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));

export function SheetModal({ fileId, title, review, locations, locationId, imageUrl, onClose, onConfirmed }: {
  fileId: string; title: string; review: Review;
  locations: { id: string; code: string; name: string }[]; locationId: string; imageUrl: string | null;
  onClose: (latest: Review | null) => void;
  onConfirmed: (out: { doc_type: string; doc_id: string }) => void;
}) {
  const { t } = useSession();
  const [data, setData] = useState<SheetData>(() => clone(review.data));
  const [history, setHistory] = useState<SheetData[]>([]);
  const [checks, setChecks] = useState<Review>(review);
  const [editing, setEditing] = useState(false);
  const [store, setStore] = useState(locationId);
  const [completeness, setCompleteness] = useState("PROVISIONAL");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [peek, setPeek] = useState(false);
  const grid = useRef<HTMLDivElement>(null);
  const kind = (data.kind as Kind) ?? review.kind;
  const lineKey = kind === "PAYMENT" ? "payment_lines" : "lines";
  const lines = ((data[lineKey] as Line[] | undefined) ?? []);
  const cols = kind === "PAYMENT" ? PAY_COLS : ITEM_COLS;
  const hasLines = kind !== "BANK" && kind !== "OTHER";

  /** every change is one undo step */
  const change = (next: SheetData) => {
    setHistory((h) => [...h.slice(-99), data]);
    setData(next);
  };
  const undo = useCallback(() => {
    setHistory((h) => {
      if (!h.length) return h;
      setData(h[h.length - 1]);
      return h.slice(0, -1);
    });
  }, []);

  // the server's checks follow the edits (nothing is saved until Confirm)
  useEffect(() => {
    const timer = setTimeout(() => {
      api<Review>(`/inbox/${fileId}/review`, { method: "POST", body: { kind, data } })
        .then((r) => {
          setChecks(r);
          // fill in what the server matched (an item from a typed barcode, a supplier from a name)
          setData((cur) => {
            let changed = false;
            const out = clone(cur);
            if (!out.supplier_id && r.data.supplier_id) { out.supplier_id = r.data.supplier_id; out.supplier_name = r.data.supplier_name; changed = true; }
            const mine = (out[lineKey] as Line[] | undefined) ?? [];
            const theirs = (r.data[lineKey] as Line[] | undefined) ?? [];
            mine.forEach((ln, i) => {
              const m = theirs[i];
              if (m && !ln.item_id && m.item_id) { ln.item_id = m.item_id; ln.item_code = m.item_code; ln.uom = ln.uom || m.uom; changed = true; }
            });
            return changed ? out : cur;
          });
        })
        .catch(() => undefined);
    }, 400);
    return () => clearTimeout(timer);
  }, [data, kind, fileId, lineKey]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !(e.target instanceof HTMLInputElement && e.target.value !== e.target.defaultValue)) {
        e.preventDefault();
        undo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo]);

  const setHead = (key: string, value: unknown) => change({ ...data, [key]: value === "" ? null : value });
  const setBank = (key: string, value: unknown) =>
    change({ ...data, bank: { ...((data.bank as Record<string, unknown>) ?? {}), [key]: value === "" ? null : value } });
  const setCell = (row: number, key: string, value: string | null) => {
    const next = clone(data);
    const ls = ((next[lineKey] as Line[]) ?? []);
    ls[row] = { ...ls[row], [key]: value === "" ? null : value };
    next[lineKey] = ls;
    change(next);
  };
  const addRow = () => change({ ...data, [lineKey]: [...lines, {}] });
  const removeRow = (row: number) => change({ ...data, [lineKey]: lines.filter((_, i) => i !== row) });

  const flag = (path: string) => FLAG[checks.fields[path]] ?? "none";
  const searchItems = useCallback((q: string) =>
    api<Page<{ id: string; code: string; name: string; base_uom: string }>>("/items", { query: { q, page_size: 8 } }).then((p) => p.items), []);
  const searchSuppliers = useCallback((q: string) =>
    api<Page<{ id: string; code: string; name: string }>>("/suppliers", { query: { q, page_size: 8 } }).then((p) => p.items), []);

  /** Enter / arrows move between cells like a spreadsheet */
  const move = (e: KeyboardEvent<HTMLElement>) => {
    const el = e.target as HTMLElement;
    const r = Number(el.dataset.r), c = Number(el.dataset.c);
    if (Number.isNaN(r)) return;
    const to = e.key === "Enter" || e.key === "ArrowDown" ? [r + 1, c] : e.key === "ArrowUp" ? [r - 1, c] : null;
    if (!to) return;
    const target = grid.current?.querySelector<HTMLElement>(`[data-r="${to[0]}"][data-c="${to[1]}"]`);
    if (target) { e.preventDefault(); target.focus(); }
  };

  const confirm = async () => {
    setBusy(true); setError(null);
    try {
      const out = await api<{ doc_type: string; doc_id: string }>(`/inbox/${fileId}/confirm`, {
        method: "POST",
        body: { kind, data, location_id: store || null, ...(kind === "CN" ? { completeness } : {}) },
      });
      onConfirmed(out);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const cancel = () => {
    if (history.length && !window.confirm(t("scanDropChanges"))) return;
    onClose(history.length ? null : checks);
  };

  const cellInput = (value: string, onCommit: (v: string) => void, opts: { r: number; c: number; type?: string; label: string; options?: string[] }) => {
    if (!editing) return <span className="cell-view">{value || "—"}</span>;
    if (opts.type === "select") {
      return (
        <select className="cell-in" value={value} aria-label={opts.label} data-r={opts.r} data-c={opts.c}
                onKeyDown={move} onChange={(e) => onCommit(e.target.value)}>
          {(opts.options ?? []).map((o) => <option key={o} value={o}>{o || "—"}</option>)}
        </select>
      );
    }
    return (
      <input key={value} className={`cell-in${opts.type === "num" ? " num" : ""}`} defaultValue={value}
             type={opts.type === "date" ? "date" : "text"} inputMode={opts.type === "num" ? "decimal" : undefined}
             aria-label={opts.label} data-r={opts.r} data-c={opts.c} onKeyDown={move}
             onBlur={(e) => { if (e.target.value !== value) onCommit(e.target.value); }} />
    );
  };

  const headRow = (label: string, path: string, body: ReactNode) => (
    <tr key={path}><th scope="row">{label}</th><td className={`flag-${flag(path)}`}>{body}</td></tr>
  );
  const bank = (data.bank as Record<string, string | null> | undefined) ?? {};
  const tally = lines.reduce((s, l) => s + (Number(l.amount) || (Number(l.qty) || 0) * (Number(l.unit_price) || 0)), 0);

  return (
    <div className="modal-bg sheet-bg" role="dialog" aria-modal="true" aria-label={t("scanSheetTitle")}>
      <div className="sheet-modal">
        <header className="sheet-top">
          <div>
            <strong>{t("scanSheetTitle")}</strong>
            <span className="muted"> · {t(`scanKind_${kind}` as Key)} · {title}</span>
          </div>
          <div className="sheet-tools">
            {imageUrl && <button className="chip-btn" onClick={() => setPeek((v) => !v)}>{t(peek ? "scanHideOriginal" : "scanShowOriginal")}</button>}
            <button className={`btn ${editing ? "" : "ghost"}`} onClick={() => setEditing((v) => !v)} aria-pressed={editing}>
              {t(editing ? "scanDoneEditing" : "scanEdit")}
            </button>
            <button className="btn ghost" disabled={!history.length} onClick={undo} title="Ctrl+Z">
              {t("scanUndo")}{history.length ? ` (${history.length})` : ""}
            </button>
            <button className="btn ghost" onClick={cancel}>{t("cancel")}</button>
            <button className="btn green" disabled={busy || !checks.ready} onClick={confirm}
                    title={checks.ready ? "" : t("scanNotReady")}>{t("scanConfirm")}</button>
          </div>
        </header>

        <div className={`sheet-body${peek ? " with-peek" : ""}`}>
          {peek && imageUrl && <div className="sheet-peek"><img src={imageUrl} alt="" /></div>}
          <div className="sheet-main" ref={grid}>
            <table className="sheet head-sheet">
              <tbody>
                {headRow(t("scanKindLabel"), "kind",
                  editing ? (
                    <select className="cell-in" value={kind} aria-label={t("scanKindLabel")}
                            onChange={(e) => change({ ...data, kind: e.target.value as Kind })}>
                      {(["INVOICE", "CN", "RETURN_NOTE", "PAYMENT", "BANK"] as Kind[]).map((k) =>
                        <option key={k} value={k}>{t(`scanKind_${k}` as Key)}</option>)}
                    </select>
                  ) : <span className="cell-view">{t(`scanKind_${kind}` as Key)}</span>)}
                {kind !== "BANK" && headRow(t("supplier"), "supplier",
                  editing ? (
                    <Picker<{ id: string; code: string; name: string }> search={searchSuppliers} keepText
                      initial={str(data.supplier_name)} label={(s) => `${s.code} · ${s.name}`} placeholder={t("searchParty")}
                      onPick={(s) => change({ ...data, supplier_id: s.id, supplier_name: s.name })} />
                  ) : <span className="cell-view">{str(data.supplier_name) || "—"}</span>)}
                {kind !== "BANK" && headRow(t("scanDocNo"), "doc_no", cellInput(str(data.doc_no), (v) => setHead("doc_no", v), { r: -10, c: 0, label: t("scanDocNo") }))}
                {headRow(t("date"), "doc_date", cellInput(str(data.doc_date), (v) => setHead("doc_date", v), { r: -9, c: 0, type: "date", label: t("date") }))}
                {(kind === "CN" || kind === "RETURN_NOTE") && headRow(t("refInvoiceNo"), "ref_invoice_no",
                  cellInput(str(data.ref_invoice_no), (v) => setHead("ref_invoice_no", v), { r: -8, c: 0, label: t("refInvoiceNo") }))}
                {kind === "BANK" && (
                  <>
                    {headRow(t("amount"), "bank.amount", cellInput(str(bank.amount), (v) => setBank("amount", v), { r: -7, c: 0, type: "num", label: t("amount") }))}
                    {headRow(t("scanDirection"), "bank.direction", cellInput(str(bank.direction), (v) => setBank("direction", v), { r: -6, c: 0, type: "select", options: ["OUT", "IN"], label: t("scanDirection") }))}
                    {headRow(t("scanCounterparty"), "bank.counterparty", cellInput(str(bank.counterparty), (v) => setBank("counterparty", v), { r: -5, c: 0, label: t("scanCounterparty") }))}
                    {headRow(t("bankRef"), "bank.reference", cellInput(str(bank.reference), (v) => setBank("reference", v), { r: -4, c: 0, label: t("bankRef") }))}
                  </>
                )}
                {kind !== "BANK" && kind !== "PAYMENT" && headRow(t("subtotalLabel"), "subtotal", cellInput(str(data.subtotal), (v) => setHead("subtotal", v), { r: -3, c: 0, type: "num", label: t("subtotalLabel") }))}
                {kind !== "BANK" && kind !== "PAYMENT" && headRow(t("taxLabel"), "tax_total", cellInput(str(data.tax_total), (v) => setHead("tax_total", v), { r: -2, c: 0, type: "num", label: t("taxLabel") }))}
                {kind !== "BANK" && headRow(t("total"), "total", cellInput(str(data.total), (v) => setHead("total", v), { r: -1, c: 0, type: "num", label: t("total") }))}
                {kind !== "BANK" && headRow(t("store"), "store",
                  <select className="cell-in" value={store} aria-label={t("store")} onChange={(e) => setStore(e.target.value)}>
                    {locations.map((l) => <option key={l.id} value={l.id}>{l.code} · {l.name}</option>)}
                  </select>)}
                {kind === "CN" && headRow(t("completeness"), "completeness",
                  <select className="cell-in" value={completeness} aria-label={t("completeness")} onChange={(e) => setCompleteness(e.target.value)}>
                    {["RECORDED", "PROVISIONAL", "COMPLETE"].map((c) => <option key={c} value={c}>{t(`comp${c}` as Key)}</option>)}
                  </select>)}
              </tbody>
            </table>

            {hasLines && (
              <div className="scroll-x">
                <table className="sheet line-sheet">
                  <thead>
                    <tr>
                      <th className="rownum">#</th>
                      {cols.map((c) => <th key={c.key} style={{ minWidth: c.width }}>{t(c.label)}</th>)}
                      {editing && <th aria-label={t("remove")} />}
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((ln, r) => (
                      <tr key={r}>
                        <td className="rownum">{r + 1}</td>
                        {cols.map((c, ci) => {
                          const path = `${lineKey}.${r}.${c.key}`;
                          if (c.type === "item") {
                            return (
                              <td key={c.key} className={`flag-${flag(path)}`}>
                                {editing ? (
                                  <Picker<{ id: string; code: string; name: string; base_uom: string }> search={searchItems} keepText
                                    initial={str(ln.item_code)} label={(i) => `${i.code} · ${i.name}`} placeholder={t("scanPickItem")}
                                    onPick={(i) => {
                                      const next = clone(data);
                                      const ls = next[lineKey] as Line[];
                                      ls[r] = { ...ls[r], item_id: i.id, item_code: i.code, uom: ls[r].uom || i.base_uom };
                                      change(next);
                                    }} />
                                ) : <span className="cell-view">{str(ln.item_code) || "—"}</span>}
                              </td>
                            );
                          }
                          return (
                            <td key={c.key} className={`flag-${flag(path)}${c.type === "num" ? " num" : ""}`}>
                              {cellInput(str(ln[c.key]), (v) => setCell(r, c.key, v),
                                { r, c: ci, type: c.type, options: c.options, label: `${t(c.label)} ${r + 1}` })}
                            </td>
                          );
                        })}
                        {editing && (
                          <td><button className="chip-btn" aria-label={`${t("remove")} ${r + 1}`} onClick={() => removeRow(r)}>✕</button></td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                  {kind !== "PAYMENT" && lines.length > 0 && (
                    <tfoot><tr><td /><td colSpan={cols.length - 1} className="muted">{t("scanLinesAddUp")}</td>
                      <td className="num"><b>{tally.toFixed(2)}</b></td>{editing && <td />}</tr></tfoot>
                  )}
                </table>
                {editing && <button className="chip-btn" onClick={addRow}>+ {t("addLine")}</button>}
                {lines.length === 0 && !editing && <span className="muted">{t("scanNoLines")}</span>}
              </div>
            )}

            <div className="sheet-foot">
              <span className="legend"><i className="sw ok" />{t("scanFlagOk")}</span>
              <span className="legend"><i className="sw check" />{t("scanFlagCheck")}</span>
              <span className="legend"><i className="sw missing" />{t("scanFlagMissing")}</span>
            </div>
            {checks.findings.length > 0 && (
              <ul className="scan-findings">
                {checks.findings.map((x, i) => (
                  <li key={i}>
                    {x.line ? `${t("lineNo").replace("{n}", String(x.line))}: ` : ""}{x.message}
                    {editing && x.candidates && (
                      <span className="cands">
                        {x.candidates.map((c) => (
                          <button key={c.id} className="chip-btn" onClick={() => {
                            if (x.code === "SUPPLIER_CANDIDATES") change({ ...data, supplier_id: c.id, supplier_name: c.name });
                            else if (x.line) {
                              const next = clone(data);
                              const ls = next[lineKey] as Line[];
                              ls[x.line - 1] = { ...ls[x.line - 1], item_id: c.id, item_code: c.code ?? c.name };
                              change(next);
                            }
                          }}>{c.code ? `${c.code} · ` : ""}{c.name}</button>
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {!checks.ready && <span className="muted">{t("scanNotReady")}</span>}
            {error && <div className="error" role="alert">{error}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
