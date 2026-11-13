import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText, today } from "../docs/shared";

// Reorder list (DynaMod's stock reorder report): items at or below their reorder level per
// store - the store's own level, else the item's - with a suggested order quantity that
// already takes off what is on order. Ticked rows of one store become a draft purchase order.

interface Row {
  item_id: string; item_code: string; item_name: string; uom: string; location_id: string;
  location: string; shelf: string | null; qty: string; reorder_level: string; max_qty: string | null;
  on_order: string; suggested_qty: string; last_supplier_id: string | null; last_supplier: string | null;
}
interface Supplier { id: string; code: string; name: string }

export function Reorder() {
  const { t, can } = useSession();
  const nav = useNavigate();
  const [stores, setStores] = useState<{ id: string; code: string }[]>([]);
  const [store, setStore] = useState("");
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [supplier, setSupplier] = useState("");
  const [busy, setBusy] = useState(false);
  const canPo = can("pur.doc.create");
  const key = (r: Row) => `${r.location_id}|${r.item_id}`;

  useEffect(() => {
    api<Page<{ id: string; code: string }>>("/locations").then((p) => setStores(p.items)).catch(() => undefined);
    if (canPo) {
      api<Page<Supplier>>("/suppliers", { query: { page_size: 200 } })
        .then((p) => setSuppliers(p.items)).catch(() => undefined);
    }
  }, [canPo]);
  useEffect(() => {
    setRows(null);
    setError(null);
    setTicked(new Set());
    api<{ rows: Row[] }>("/reports/reorder", { query: store ? { location_id: store } : {} })
      .then((r) => setRows(r.rows))
      .catch((e) => { setRows([]); setError(errorText(e)); });
  }, [store]);

  const toggle = (r: Row) => {
    const next = new Set(ticked);
    if (next.has(key(r))) next.delete(key(r)); else next.add(key(r));
    setTicked(next);
    // default supplier: whoever last delivered the first ticked item
    const first = (rows ?? []).find((x) => next.has(key(x)));
    if (first?.last_supplier_id && (!supplier || next.size === 1)) setSupplier(first.last_supplier_id);
  };

  const makePo = async () => {
    const picked = (rows ?? []).filter((r) => ticked.has(key(r)) && Number(r.suggested_qty) > 0);
    const locs = new Set(picked.map((r) => r.location_id));
    if (locs.size !== 1) return setError(t("pickOneStore"));
    if (!supplier) return setError(t("pickSupplier"));
    setBusy(true);
    setError(null);
    try {
      const po = await api<{ id: string }>("/purchase/purchase-order", {
        method: "POST",
        body: {
          doc_date: today(), supplier_id: supplier, location_id: picked[0].location_id,
          lines: picked.map((r) => ({ item_id: r.item_id, uom: r.uom, qty: r.suggested_qty })),
        },
      });
      nav(`/docs/purchase-order/${po.id}`);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

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
      {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0
        ? <span className="muted">{t("nothingToReorder")}</span> : (
          <div className="scroll-x">
            <table className="table doc-lines">
              <thead>
                <tr>
                  {canPo && <th />}
                  <th>{t("store")}</th><th>{t("code")}</th><th>{t("item")}</th><th>{t("shelf")}</th>
                  <th className="num">{t("onHand")}</th><th className="num">{t("onOrder")}</th>
                  <th className="num">{t("reorderLevel")}</th><th className="num">{t("maxQty")}</th>
                  <th className="num">{t("suggestedQty")}</th><th>{t("lastSupplier")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={key(r)}>
                    {canPo && (
                      <td>
                        <input type="checkbox" checked={ticked.has(key(r))} disabled={Number(r.suggested_qty) <= 0}
                               aria-label={`${r.location} ${r.item_code}`} onChange={() => toggle(r)} />
                      </td>
                    )}
                    <td>{r.location}</td>
                    <td><Link to={`/items/edit/${r.item_id}`}>{r.item_code}</Link></td>
                    <td>{r.item_name}</td>
                    <td>{r.shelf ?? "—"}</td>
                    <td className="num">
                      <span className={`badge ${Number(r.qty) < 0 ? "red" : "amber"}`}>{qty(r.qty)}</span>
                    </td>
                    <td className="num">{Number(r.on_order) ? qty(r.on_order) : "—"}</td>
                    <td className="num">{qty(r.reorder_level)}</td>
                    <td className="num">{r.max_qty != null ? qty(r.max_qty) : "—"}</td>
                    <td className="num"><b>{qty(r.suggested_qty)}</b> {r.uom}</td>
                    <td>{r.last_supplier ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {canPo && ticked.size > 0 && (
        <div className="pick-box">
          <strong>{t("makePo")}</strong>
          <span className="muted">{t("makePoHint")}</span>
          <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
            <select className="input" style={{ maxWidth: 320 }} value={supplier} aria-label={t("pickSupplier")}
                    onChange={(e) => setSupplier(e.target.value)}>
              <option value="">{t("pickSupplier")}…</option>
              {suppliers.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
            </select>
            <button className="btn" disabled={busy} onClick={makePo}>{t("makePo")}</button>
          </div>
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}
