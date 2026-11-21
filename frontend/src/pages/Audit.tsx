import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText } from "../docs/shared";

// Audit trail: every change anyone (or the AI butler / an MCP client) made, newest first,
// with who, from which computer, and the fields before and after. Read only.

interface Event {
  id: number;
  at: string;
  actor_type: string;
  actor_name: string | null;
  device_name: string | null;
  action: string;
  entity: string;
  entity_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

// record types people recognise, and where each one opens
const ENTITY: Record<string, { label: string; route?: (id: string) => string }> = {
  "sal.quotation": { label: "Quotation", route: (id) => `/docs/quotation/${id}` },
  "sal.sales_order": { label: "Sales order", route: (id) => `/docs/sales-order/${id}` },
  "sal.do": { label: "Delivery order", route: (id) => `/docs/delivery-order/${id}` },
  "sal.invoice": { label: "Sales invoice", route: (id) => `/docs/sales-invoice/${id}` },
  "sal.customer_cn": { label: "Customer credit note", route: (id) => `/docs/customer-cn/${id}` },
  "sal.customer_dn": { label: "Customer debit note", route: (id) => `/docs/customer-dn/${id}` },
  "sal.einvoice": { label: "E-invoice" },
  "pur.purchase_order": { label: "Purchase order", route: (id) => `/docs/purchase-order/${id}` },
  "pur.grn": { label: "Goods received", route: (id) => `/docs/grn/${id}` },
  "pur.supplier_cn": { label: "Supplier credit note", route: (id) => `/docs/supplier-cn/${id}` },
  "pur.supplier_dn": { label: "Supplier debit note", route: (id) => `/docs/supplier-dn/${id}` },
  "pur.supplier_adj": { label: "Supplier adjustment", route: (id) => `/docs/supplier-adj/${id}` },
  "pur.inbox_file": { label: "Scanned paper" },
  "fin.receipt": { label: "Receipt", route: (id) => `/docs/receipt/${id}` },
  "fin.supplier_payment": { label: "Supplier payment", route: (id) => `/docs/supplier-payment/${id}` },
  "fin.supplier_refund": { label: "Supplier refund", route: (id) => `/docs/supplier-refund/${id}` },
  "fin.knockoff": { label: "Knock-off" },
  "md.item": { label: "Item", route: (id) => `/items/edit/${id}` },
  "md.customer": { label: "Customer", route: (id) => `/parties/customer/${id}` },
  "md.supplier": { label: "Supplier", route: (id) => `/parties/supplier/${id}` },
  "md.tax_code": { label: "Tax code" },
  "md.uom": { label: "Unit" },
  "md.item_category": { label: "Item category" },
  "md.item_attribute": { label: "Item group / brand" },
  "acc.journal": { label: "Journal", route: () => "/accounting" },
  "acc.account": { label: "Ledger account", route: () => "/accounting" },
  "core.user": { label: "User", route: () => "/settings?tab=users" },
  "core.device": { label: "Computer", route: () => "/settings?tab=devices" },
  "core.company": { label: "Company", route: () => "/settings?tab=company" },
  "core.user_session": { label: "Sign-in / token" },
  "core.doc_issue": { label: "Problem document", route: () => "/issues" },
  "ai.kb_document": { label: "Knowledge document" },
};
const entityLabel = (e: string) => ENTITY[e]?.label ?? e;
const ACTOR_BADGE: Record<string, string> = { ai: "AI butler", mcp: "MCP", system: "System" };

/** The name a person would use for the record: document number, code, or name. */
function recordName(e: Event): string {
  for (const side of [e.after, e.before]) {
    for (const k of ["doc_no", "code", "username", "name", "display_name"]) {
      const v = side?.[k];
      if (typeof v === "string" && v) return v;
    }
  }
  return e.entity_id ? e.entity_id.slice(0, 8) : "";
}

const show = (v: unknown) =>
  v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);

function Changes({ e }: { e: Event }) {
  const { t } = useSession();
  const keys = [...new Set([...Object.keys(e.before ?? {}), ...Object.keys(e.after ?? {})])];
  if (!keys.length) return <span className="muted">{t("auditNoFields")}</span>;
  const both = e.before !== null && e.after !== null;
  return (
    <table className="table audit-changes">
      <thead>
        <tr><th>{t("auditField")}</th>{e.before && <th>{t("auditBefore")}</th>}{e.after && <th>{t("auditAfter")}</th>}</tr>
      </thead>
      <tbody>
        {keys.map((k) => (
          <tr key={k}>
            <td className="muted">{k}</td>
            {e.before && <td className={both ? "was" : ""}>{show(e.before[k])}</td>}
            {e.after && <td>{show(e.after[k])}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function AuditTrail() {
  const { t } = useSession();
  const [f, setF] = useState({ date_from: "", date_to: "", actor_id: "", entity: "", action: "", q: "" });
  const [facets, setFacets] = useState<{ actions: string[]; entities: string[] }>({ actions: [], entities: [] });
  const [users, setUsers] = useState<{ id: string; display_name: string }[]>([]);
  const [rows, setRows] = useState<Event[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ actions: string[]; entities: string[] }>("/audit/facets").then(setFacets).catch(() => undefined);
    api<Page<{ id: string; display_name: string }>>("/users?page_size=200")
      .then((x) => setUsers(x.items)).catch(() => undefined);
  }, []);

  // filters change: start again at page 1 (debounced for typing in the search box)
  useEffect(() => {
    const timer = setTimeout(() => {
      const qs = new URLSearchParams({ page: String(page), page_size: "50" });
      Object.entries(f).forEach(([k, v]) => { if (v.trim()) qs.set(k, v.trim()); });
      api<Page<Event>>(`/audit/events?${qs}`)
        .then((x) => {
          setRows((old) => (page === 1 ? x.items : [...(old ?? []), ...x.items]));
          setTotal(x.total);
          setError(null);
        })
        .catch((e) => { setRows([]); setError(errorText(e)); });
    }, f.q ? 250 : 0);
    return () => clearTimeout(timer);
  }, [f, page]);

  const set = (k: keyof typeof f) => (v: string) => { setPage(1); setOpen(null); setF({ ...f, [k]: v }); };
  const when = (iso: string) =>
    new Date(iso).toLocaleString("en-MY", { dateStyle: "medium", timeStyle: "medium" });

  return (
    <section className="card glass">
      <span className="muted">{t("auditHint")}</span>
      <div className="doc-head audit-filters">
        <div className="field"><label htmlFor="au-from">{t("dateFrom")}</label>
          <input id="au-from" type="date" className="input" value={f.date_from}
                 onChange={(e) => set("date_from")(e.target.value)} /></div>
        <div className="field"><label htmlFor="au-to">{t("dateTo")}</label>
          <input id="au-to" type="date" className="input" value={f.date_to}
                 onChange={(e) => set("date_to")(e.target.value)} /></div>
        <div className="field"><label htmlFor="au-who">{t("auditWho")}</label>
          <select id="au-who" className="input" value={f.actor_id} onChange={(e) => set("actor_id")(e.target.value)}>
            <option value="">{t("auditEveryone")}</option>
            {users.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </select></div>
        <div className="field"><label htmlFor="au-what">{t("auditRecord")}</label>
          <select id="au-what" className="input" value={f.entity} onChange={(e) => set("entity")(e.target.value)}>
            <option value="">{t("auditAll")}</option>
            {facets.entities.map((x) => <option key={x} value={x}>{entityLabel(x)}</option>)}
          </select></div>
        <div className="field"><label htmlFor="au-act">{t("auditAction")}</label>
          <select id="au-act" className="input" value={f.action} onChange={(e) => set("action")(e.target.value)}>
            <option value="">{t("auditAll")}</option>
            {facets.actions.map((x) => <option key={x} value={x}>{x.replace(/_/g, " ")}</option>)}
          </select></div>
        <div className="field wide"><label htmlFor="au-q">{t("auditSearch")}</label>
          <input id="au-q" className="input" value={f.q} placeholder="INV000003, Milo, …"
                 onChange={(e) => set("q")(e.target.value)} /></div>
      </div>
      {error && <div className="error" role="alert">{error}</div>}
      {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0 ? (
        <span className="muted">{t("auditNone")}</span>
      ) : (
        <div className="scroll-x">
          <table className="table one-line audit-table">
            <thead>
              <tr><th>{t("auditWhen")}</th><th>{t("auditWho")}</th><th>{t("auditAction")}</th>
                <th>{t("auditRecord")}</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const route = e.entity_id ? ENTITY[e.entity]?.route?.(e.entity_id) : undefined;
                const isOpen = open === e.id;
                return (
                  <Fragment key={e.id}>
                    <tr className="click" onClick={() => setOpen(isOpen ? null : e.id)}>
                      <td className="num-left">{when(e.at)}</td>
                      <td>
                        {e.actor_name ?? "—"}
                        {ACTOR_BADGE[e.actor_type] && <span className="badge audit-badge">{ACTOR_BADGE[e.actor_type]}</span>}
                        {e.device_name && <div className="muted small">{e.device_name}</div>}
                      </td>
                      <td><span className={`audit-action a-${e.action}`}>{e.action.replace(/_/g, " ")}</span></td>
                      <td>
                        <span className="muted">{entityLabel(e.entity)}</span>{" "}
                        {route ? <Link to={route} onClick={(ev) => ev.stopPropagation()}>{recordName(e)}</Link>
                          : recordName(e)}
                      </td>
                      <td className="num muted" aria-hidden="true">{isOpen ? "▴" : "▾"}</td>
                    </tr>
                    {isOpen && (
                      <tr className="sub-row"><td colSpan={5}><Changes e={e} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {rows && rows.length < total && (
        <button className="btn ghost" onClick={() => setPage(page + 1)}>
          {t("auditMore")} ({rows.length} / {total})
        </button>
      )}
    </section>
  );
}
