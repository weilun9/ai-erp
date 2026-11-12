import { useEffect, useState } from "react";
import { api } from "../api";
import { useSession } from "../session";

// The paper version of a document: only visible when printing (or saving as PDF from the
// browser's print dialog). The entry screens fill it from the saved document, so what is
// printed is what the server holds, never unsaved edits.

interface Company {
  name: string; tin: string | null; sst_no: string | null; id_no: string | null;
  address1: string | null; address2: string | null; address3: string | null;
  city: string | null; postcode: string | null; phone: string | null; email: string | null;
}
export interface PrintParty {
  code?: string; name: string; tin?: string | null; sst_no?: string | null;
  address1?: string | null; address2?: string | null; address3?: string | null;
  city?: string | null; postcode?: string | null; phone?: string | null;
  contact_person?: string | null;
}
export interface PrintColumn { label: string; num?: boolean; width?: string }

interface Props {
  title: string;
  docNo: string | null;
  status: string;
  partyLabel: string;
  party: PrintParty | null;
  meta: [string, string | null | undefined][];
  columns: PrintColumn[];
  rows: (string | number | null | undefined)[][];
  totals: [string, string, boolean?][];        // label, value, emphasised
  remark?: string | null;
  signatures?: string[];
}

let companyOnce: Promise<Company | null> | null = null;
const loadCompany = () =>
  (companyOnce ??= api<Company>("/company").catch(() => null));

const lines = (...parts: (string | null | undefined)[]) => parts.filter((x) => x && x.trim());

export function PrintSheet(p: Props) {
  const { t } = useSession();
  const [company, setCompany] = useState<Company | null>(null);
  useEffect(() => {
    loadCompany().then(setCompany);
  }, []);
  const c = company;
  const notFinal = p.status === "DRAFT" ? t("printDraft") : p.status === "VOIDED" ? t("printVoided") : null;

  return (
    <div className="print-only sheet">
      <header className="sheet-head">
        <div>
          <div className="sheet-company">{c?.name ?? t("appName")}</div>
          {c && lines(c.address1, c.address2, c.address3, [c.postcode, c.city].filter(Boolean).join(" "))
            .map((l, i) => <div key={i}>{l}</div>)}
          {c && (
            <div className="sheet-small">
              {lines(c.id_no && `Reg. ${c.id_no}`, c.tin && `TIN ${c.tin}`, c.sst_no && `SST ${c.sst_no}`,
                c.phone && `Tel ${c.phone}`, c.email).join(" · ")}
            </div>
          )}
        </div>
        <div className="sheet-title">
          <div className="sheet-kind">{p.title}</div>
          <div className="sheet-no">{p.docNo ?? "—"}</div>
          {notFinal && <div className="sheet-flag">{notFinal}</div>}
        </div>
      </header>

      <section className="sheet-parties">
        <div>
          <div className="sheet-label">{p.partyLabel}</div>
          {p.party ? (
            <>
              <strong>{p.party.name}</strong>
              {lines(p.party.address1, p.party.address2, p.party.address3,
                [p.party.postcode, p.party.city].filter(Boolean).join(" ")).map((l, i) => <div key={i}>{l}</div>)}
              <div className="sheet-small">
                {lines(p.party.tin && `TIN ${p.party.tin}`, p.party.sst_no && `SST ${p.party.sst_no}`,
                  p.party.contact_person, p.party.phone && `Tel ${p.party.phone}`).join(" · ")}
              </div>
            </>
          ) : "—"}
        </div>
        <dl className="sheet-meta">
          {p.meta.filter(([, v]) => v).map(([k, v]) => (
            <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      </section>

      <table className="sheet-table">
        <thead>
          <tr>{p.columns.map((col) => (
            <th key={col.label} className={col.num ? "num" : ""} style={col.width ? { width: col.width } : {}}>
              {col.label}
            </th>
          ))}</tr>
        </thead>
        <tbody>
          {p.rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => <td key={j} className={p.columns[j]?.num ? "num" : ""}>{cell ?? ""}</td>)}
            </tr>
          ))}
        </tbody>
      </table>

      <section className="sheet-foot">
        <div className="sheet-remark">
          {p.remark && (<><div className="sheet-label">{t("remark")}</div><div>{p.remark}</div></>)}
        </div>
        <dl className="sheet-totals">
          {p.totals.map(([k, v, strong]) => (
            <div key={k} className={strong ? "strong" : ""}><dt>{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      </section>

      {p.signatures && p.signatures.length > 0 && (
        <section className="sheet-sign">
          {p.signatures.map((s) => <div key={s}><span>{s}</span></div>)}
        </section>
      )}
      <footer className="sheet-small sheet-end">{t("printFooter")}</footer>
    </div>
  );
}
