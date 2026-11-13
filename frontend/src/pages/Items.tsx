import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, money, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";

interface Item {
  id: string;
  code: string;
  name: string;
  base_uom: string;
  uoms: { uom: string; factor: string; selling_price: string | null; purchase_price?: string | null }[];
  barcodes: { barcode: string; uom: string }[];
}
interface Balance { location_id: string; qty: string; avg_cost?: string }

export function Items() {
  const { t, can, me } = useSession();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [text, setText] = useState(q);
  const [items, setItems] = useState<Item[] | null>(null);
  const [picked, setPicked] = useState<Item | null>(null);
  const [stock, setStock] = useState<Balance[] | null>(null);
  const [stores, setStores] = useState<Record<string, string>>({});

  useEffect(() => {
    setText(q);
  }, [q]);
  useEffect(() => {
    setItems(null);
    api<Page<Item>>("/items", { query: { q, page_size: 40 } })
      .then((p) => {
        setItems(p.items);
        setPicked(p.items.length === 1 ? p.items[0] : null);
      })
      .catch(() => setItems([]));
  }, [q]);
  useEffect(() => {
    api<Page<{ id: string; code: string }>>("/locations")
      .then((p) => setStores(Object.fromEntries(p.items.map((l) => [l.id, l.code]))))
      .catch(() => undefined);
  }, [me]);
  useEffect(() => {
    setStock(null);
    if (picked && can("inv.balance.read")) {
      api<Page<Balance>>("/stock/balance", { query: { item_id: picked.id } })
        .then((p) => setStock(p.items))
        .catch(() => setStock([]));
    }
  }, [picked, can]);

  return (
    <div className="two">
      <section className="card glass">
        <form className="row" onSubmit={(e) => { e.preventDefault(); setParams(text ? { q: text } : {}); }}>
          <input className="input" style={{ flex: 1 }} value={text} placeholder={t("search")}
                 onChange={(e) => setText(e.target.value)} />
          <button className="round-btn" aria-label="search">⌕</button>
          {can("report.run") && can("inv.balance.read") && (
            <Link className="chip-btn" style={{ textDecoration: "none", whiteSpace: "nowrap" }}
                  to="/reorder">{t("reorderReport")}</Link>
          )}
          {can("md.item.manage") && (
            <Link className="btn" style={{ textDecoration: "none", whiteSpace: "nowrap" }}
                  to="/items/edit/new">+ {t("newDoc")}</Link>
          )}
        </form>
        {!items ? <span className="muted">{t("loading")}</span>
          : items.length === 0 ? <span className="muted">{t("noResults")}</span>
          : (
            <div className="scroll-x">
              <table className="table">
                <thead><tr><th>{t("code")}</th><th>{t("item")}</th><th className="num">{t("price")}</th></tr></thead>
                <tbody>
                  {items.map((i) => (
                    <tr key={i.id} className="click" onClick={() => setPicked(i)}
                        style={picked?.id === i.id ? { background: "rgba(255,255,255,0.06)" } : {}}>
                      <td>{i.code}</td>
                      <td>{i.name}</td>
                      <td className="num">{money(i.uoms.find((u) => u.uom === i.base_uom)?.selling_price)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </section>
      {picked && (
        <section className="card glass">
          <div className="row">
            <h2 style={{ margin: 0 }}>{picked.name}</h2>
            {can("md.item.manage") && (
              <Link className="chip-btn" to={`/items/edit/${picked.id}`}>{t("editItem")}</Link>
            )}
          </div>
          <span className="muted">{picked.code} · {picked.barcodes.map((b) => b.barcode).join(", ")}</span>
          <h3>{t("units")}</h3>
          <ul className="list">
            {picked.uoms.map((u) => (
              <li key={u.uom}>
                <span>{u.uom} × {qty(u.factor)}</span>
                <span>{money(u.selling_price)}{u.purchase_price != null &&
                  <span className="muted"> · cost {money(u.purchase_price)}</span>}</span>
              </li>
            ))}
          </ul>
          {can("inv.balance.read") && (
            <>
              <h3>{t("stockBy")}</h3>
              {!stock ? <span className="muted">{t("loading")}</span> : stock.length === 0
                ? <span className="muted">{t("noResults")}</span> : (
                  <ul className="list">
                    {stock.map((b) => (
                      <li key={b.location_id}>
                        <span>{stores[b.location_id] ?? "…"}</span>
                        <span>
                          <span className={`badge ${Number(b.qty) <= 0 ? "red" : Number(b.qty) <= 5 ? "amber" : "green"}`}>
                            {qty(b.qty)} {picked.base_uom}
                          </span>
                          {b.avg_cost !== undefined && <span className="muted"> · avg {money(b.avg_cost)}</span>}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
