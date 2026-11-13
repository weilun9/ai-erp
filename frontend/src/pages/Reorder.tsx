import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText } from "../docs/shared";

// Reorder list (DynaMod's stock reorder report): items at or below their reorder level per
// store - the store's own level, else the item's - with a suggested order quantity.

interface Row {
  item_id: string; item_code: string; item_name: string; uom: string; location_id: string;
  location: string; shelf: string | null; qty: string; reorder_level: string; max_qty: string | null;
  suggested_qty: string;
}

export function Reorder() {
  const { t } = useSession();
  const [stores, setStores] = useState<{ id: string; code: string }[]>([]);
  const [store, setStore] = useState("");
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Page<{ id: string; code: string }>>("/locations").then((p) => setStores(p.items)).catch(() => undefined);
  }, []);
  useEffect(() => {
    setRows(null);
    setError(null);
    api<{ rows: Row[] }>("/reports/reorder", { query: store ? { location_id: store } : {} })
      .then((r) => setRows(r.rows))
      .catch((e) => { setRows([]); setError(errorText(e)); });
  }, [store]);

  return (
    <section className="card glass">
      <div className="row">
        <div>
          <Link to="/items" className="muted" style={{ textDecoration: "none" }}>← {t("items")}</Link>
          <h2 style={{ margin: "4px 0 0" }}>{t("reorderReport")}</h2>
        </div>
        <select className="input" style={{ maxWidth: 200 }} value={store} aria-label={t("store")}
                onChange={(e) => setStore(e.target.value)}>
          <option value="">{t("allKinds")}</option>
          {stores.map((s) => <option key={s.id} value={s.id}>{s.code}</option>)}
        </select>
      </div>
      <span className="muted">{t("reorderHint")}</span>
      {error && <div className="error" role="alert">{error}</div>}
      {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0
        ? <span className="muted">{t("nothingToReorder")}</span> : (
          <div className="scroll-x">
            <table className="table doc-lines">
              <thead>
                <tr>
                  <th>{t("store")}</th><th>{t("code")}</th><th>{t("item")}</th><th>{t("shelf")}</th>
                  <th className="num">{t("onHand")}</th><th className="num">{t("reorderLevel")}</th>
                  <th className="num">{t("maxQty")}</th><th className="num">{t("suggestedQty")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.location_id}-${r.item_id}`}>
                    <td>{r.location}</td>
                    <td><Link to={`/items/edit/${r.item_id}`}>{r.item_code}</Link></td>
                    <td>{r.item_name}</td>
                    <td>{r.shelf ?? "—"}</td>
                    <td className="num">
                      <span className={`badge ${Number(r.qty) < 0 ? "red" : "amber"}`}>{qty(r.qty)}</span>
                    </td>
                    <td className="num">{qty(r.reorder_level)}</td>
                    <td className="num">{r.max_qty != null ? qty(r.max_qty) : "—"}</td>
                    <td className="num"><b>{qty(r.suggested_qty)}</b> {r.uom}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}
