import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { KINDS, PURCHASE_KINDS, SALES_KINDS, STATUS_COLOR } from "../docs/kinds";
import type { Area, Kind } from "../docs/kinds";

interface Doc {
  id: string;
  doc_no: string | null;
  doc_date: string;
  customer_id?: string;
  supplier_id?: string;
  status: string;
  total?: string | null;
  gross_profit?: string | null;
}

/** Sales or purchase documents: one tab per kind, newest first, click a row to open it. */
export function DocList({ area }: { area: Area }) {
  const { t, can } = useSession();
  const nav = useNavigate();
  const kinds: readonly Kind[] = area === "sales" ? SALES_KINDS : PURCHASE_KINDS;
  const [params, setParams] = useSearchParams();
  const kind = (kinds as readonly string[]).includes(params.get("kind") ?? "")
    ? (params.get("kind") as Kind) : kinds[0];
  const def = KINDS[kind];
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [parties, setParties] = useState<Record<string, string>>({});

  useEffect(() => {
    const path = area === "sales" ? "/customers" : "/suppliers";
    api<Page<{ id: string; name: string }>>(path, { query: { page_size: 200 } })
      .then((p) => setParties(Object.fromEntries(p.items.map((c) => [c.id, c.name]))))
      .catch(() => undefined);
  }, [area]);
  useEffect(() => {
    setDocs(null);
    api<Page<Doc>>(def.api, { query: { page_size: 50 } })
      .then((p) => setDocs(p.items))
      .catch(() => setDocs([]));
  }, [def.api]);

  const showProfit = docs?.some((d) => d.gross_profit !== undefined);
  return (
    <section className="card glass">
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="tabs">
          {kinds.map((k) => (
            <button key={k} className={k === kind ? "on" : ""}
                    onClick={() => setParams({ kind: k }, { replace: true })}>{t(k)}</button>
          ))}
        </div>
        {can(`${def.perm}.doc.create`) && (
          <Link className="btn" style={{ textDecoration: "none", whiteSpace: "nowrap" }}
                to={`/docs/${kind}/new`}>+ {t("newDoc")}</Link>
        )}
      </div>
      {!docs ? <span className="muted">{t("loading")}</span>
        : docs.length === 0 ? <span className="muted">{t("noResults")}</span>
        : (
          <div className="scroll-x">
            <table className="table">
              <thead>
                <tr>
                  <th>{t("docNo")}</th><th>{t("date")}</th>
                  <th>{t(def.party)}</th>
                  <th>{t("status")}</th><th className="num">{t("total")}</th>
                  {showProfit && <th className="num">{t("grossProfit")}</th>}
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id} className="click" onClick={() => nav(`/docs/${kind}/${d.id}`)}>
                    <td>{d.doc_no ?? <span className="muted">{t("draft")}</span>}</td>
                    <td>{d.doc_date}</td>
                    <td>{parties[(d.customer_id ?? d.supplier_id) as string] ?? "…"}</td>
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
