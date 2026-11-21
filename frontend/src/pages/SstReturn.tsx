import { useEffect, useMemo, useState } from "react";
import { api, money } from "../api";
import { useSession } from "../session";
import { errorText, today } from "../docs/shared";
import type { Key } from "../i18n";

// SST-02 working paper: per tax code, what was charged in the taxable period less credit
// notes and till refunds, and the sales / service tax payable. Fill MySST from it.

interface Row {
  code: string; name: string; rate: string; kind: "SALES" | "SERVICE" | "EXEMPT" | "NOT_TAXABLE";
  sales_value: string; sales_tax: string; less_value: string; less_tax: string;
  net_value: string; net_tax: string;
}
interface Report {
  rows: Row[]; taxable_sales_value: string; taxable_service_value: string; exempt_value: string;
  not_taxable_value: string; sales_tax_payable: string; service_tax_payable: string;
  total_payable: string; documents: Record<string, number>;
}
const KIND_LABEL: Record<Row["kind"], Key> = {
  SALES: "sstSalesTax", SERVICE: "sstServiceTax", EXEMPT: "sstExempt", NOT_TAXABLE: "sstNotTaxable",
};
const DOC_LABEL: Record<string, Key> = {
  invoices: "sstDocInvoices", debit_notes: "sstDocDn", till_sales: "sstDocTill",
  credit_notes: "sstDocCn", till_refunds: "sstDocRefunds",
};

/** Two-month taxable periods (Jan-Feb, Mar-Apr...) of a year. */
function periods(year: number) {
  return Array.from({ length: 6 }, (_, i) => {
    const m1 = i * 2 + 1, m2 = m1 + 1;
    const last = new Date(year, m2, 0).getDate();
    const pad = (n: number) => String(n).padStart(2, "0");
    return { label: `${pad(m1)}–${pad(m2)}/${year}`, from: `${year}-${pad(m1)}-01`, to: `${year}-${pad(m2)}-${last}` };
  });
}

export function SstReturn() {
  const { t } = useSession();
  const year = Number(today().slice(0, 4));
  const options = useMemo(() => [...periods(year - 1), ...periods(year)], [year]);
  // default: the last period that has ended (the one due for filing)
  const current = options.findIndex((o) => o.from <= today() && today() <= o.to);
  const [from, setFrom] = useState(options[Math.max(0, current - 1)].from);
  const [to, setTo] = useState(options[Math.max(0, current - 1)].to);
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api<Report>("/reports/sst-return", { query: { date_from: from, date_to: to } })
      .then((r) => { setData(r); setError(null); }).catch((e) => setError(errorText(e)));
  }, [from, to]);

  const pick = options.find((o) => o.from === from && o.to === to)?.label ?? "";
  return (
    <section className="card glass">
      <span className="muted no-print">{t("sstHint")}</span>
      <div className="doc-head no-print">
        <div className="field">
          <label htmlFor="sst-period">{t("sstPeriod")}</label>
          <select id="sst-period" className="input" value={pick}
                  onChange={(e) => { const o = options.find((x) => x.label === e.target.value); if (o) { setFrom(o.from); setTo(o.to); } }}>
            {!pick && <option value="">{t("sstCustom")}</option>}
            {options.map((o) => <option key={o.label} value={o.label}>{o.label}</option>)}
          </select>
        </div>
        <div className="field"><label htmlFor="sst-from">{t("dateFrom")}</label>
          <input id="sst-from" type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
        <div className="field"><label htmlFor="sst-to">{t("dateTo")}</label>
          <input id="sst-to" type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} /></div>
      </div>
      <h3 style={{ margin: 0 }}>{t("sstTitle")} {from} – {to}</h3>
      {error && <div className="error" role="alert">{error}</div>}
      {!data ? <span className="muted">{t("loading")}</span> : (
        <>
          <div className="rec-sum">
            <div><span>{t("sstSalesTax")}</span><b>{money(data.sales_tax_payable)}</b>
              <span className="small muted">{t("sstOn")} {money(data.taxable_sales_value)}</span></div>
            <div><span>{t("sstServiceTax")}</span><b>{money(data.service_tax_payable)}</b>
              <span className="small muted">{t("sstOn")} {money(data.taxable_service_value)}</span></div>
            <div className="grand"><span>{t("sstPayable")}</span><b>{money(data.total_payable)}</b></div>
          </div>
          <div className="scroll-x">
            <table className="table doc-lines">
              <thead>
                <tr><th>{t("tax")}</th><th className="num">%</th>
                  <th className="num">{t("sstCharged")}</th><th className="num">{t("sstTaxCharged")}</th>
                  <th className="num">{t("sstLess")}</th><th className="num">{t("sstLessTax")}</th>
                  <th className="num">{t("sstNetValue")}</th><th className="num">{t("sstNetTax")}</th></tr>
              </thead>
              <tbody>
                {data.rows.map((r, i) => (
                  <tr key={r.code} className={i > 0 && data.rows[i - 1].kind !== r.kind ? "grand-row" : ""}>
                    <td>{r.code} <span className="muted">· {t(KIND_LABEL[r.kind])}</span></td>
                    <td className="num">{Number(r.rate)}</td>
                    <td className="num">{money(r.sales_value)}</td><td className="num">{money(r.sales_tax)}</td>
                    <td className="num">{money(r.less_value)}</td><td className="num">{money(r.less_tax)}</td>
                    <td className="num"><b>{money(r.net_value)}</b></td><td className="num"><b>{money(r.net_tax)}</b></td>
                  </tr>
                ))}
                {data.rows.length === 0 && <tr><td colSpan={8} className="muted">{t("sstNothing")}</td></tr>}
              </tbody>
            </table>
          </div>
          <span className="muted small">
            {Object.entries(data.documents).map(([k, n]) => `${t(DOC_LABEL[k] ?? "document")}: ${n}`).join(" · ")}
          </span>
        </>
      )}
    </section>
  );
}
