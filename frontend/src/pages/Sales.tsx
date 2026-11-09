import { useEffect, useState } from "react";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";

const KINDS = ["sales-invoice", "delivery-order", "quotation", "customer-cn"] as const;
type Kind = (typeof KINDS)[number];

interface Doc {
  id: string;
  doc_no: string | null;
  doc_date: string;
  customer_id: string;
  status: string;
  total: string;
  gross_profit?: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  POSTED: "green", SAVED: "amber", DRAFT: "", VOIDED: "red",
};

export function Sales() {
  const { t } = useSession();
  const [kind, setKind] = useState<Kind>("sales-invoice");
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [customers, setCustomers] = useState<Record<string, string>>({});

  useEffect(() => {
    api<Page<{ id: string; code: string; name: string }>>("/customers", { query: { page_size: 200 } })
      .then((p) => setCustomers(Object.fromEntries(p.items.map((c) => [c.id, c.name]))))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    setDocs(null);
    api<Page<Doc>>(`/sales/${kind}`, { query: { page_size: 50 } })
      .then((p) => setDocs(p.items))
      .catch(() => setDocs([]));
  }, [kind]);

  const showProfit = docs?.some((d) => d.gross_profit !== undefined);
  return (
    <section className="card glass">
      <div className="tabs">
        {KINDS.map((k) => (
          <button key={k} className={k === kind ? "on" : ""} onClick={() => setKind(k)}>{t(k)}</button>
        ))}
      </div>
      {!docs ? <span className="muted">{t("loading")}</span>
        : docs.length === 0 ? <span className="muted">{t("noResults")}</span>
        : (
          <div className="scroll-x">
            <table className="table">
              <thead>
                <tr>
                  <th>{t("docNo")}</th><th>{t("date")}</th><th>{t("customer")}</th>
                  <th>{t("status")}</th><th className="num">{t("total")}</th>
                  {showProfit && <th className="num">{t("grossProfit")}</th>}
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.doc_no ?? "—"}</td>
                    <td>{d.doc_date}</td>
                    <td>{customers[d.customer_id] ?? "…"}</td>
                    <td><span className={`badge ${STATUS_COLOR[d.status] ?? ""}`}>{d.status}</span></td>
                    <td className="num">{money(d.total)}</td>
                    {showProfit && <td className="num">{d.gross_profit ? money(d.gross_profit) : "—"}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}
