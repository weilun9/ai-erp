import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText, isNum } from "../docs/shared";

// Create or edit an item: identity, tax codes, the units it is bought and sold in (each with
// its factor to the base unit and its prices) and its barcodes (each tied to one unit, so a
// scanned carton code sells a carton).

interface ItemUom { uom: string; factor: string; selling_price: string | null; purchase_price?: string | null }
interface Item {
  id: string; code: string; name: string; short_name: string | null; category_id: string | null;
  base_uom: string; purchase_tax_code_id: string | null; sales_tax_code_id: string | null;
  einvoice_classification: string; track_batch: boolean; is_active: boolean; version: number;
  uoms: ItemUom[]; barcodes: { barcode: string; uom: string }[];
}
interface Opt { id: string; code: string; name: string }
interface UomRow { key: number; uom: string; factor: string; selling_price: string; purchase_price: string }
interface CodeRow { key: number; barcode: string; uom: string }

let nextKey = 1;
const num = (v: string | null | undefined) => (v === null || v === undefined ? "" : String(Number(v)));

export function ItemForm() {
  const { id } = useParams();
  const { t, can } = useSession();
  const nav = useNavigate();
  const isNew = id === "new";
  const editable = can("md.item.manage");

  const [loaded, setLoaded] = useState<Item | null>(null);
  const [f, setF] = useState({
    code: "", name: "", short_name: "", category_id: "", base_uom: "", sales_tax_code_id: "",
    purchase_tax_code_id: "", einvoice_classification: "022", track_batch: false, is_active: true,
  });
  const [uoms, setUoms] = useState<UomRow[]>([]);
  const [codes, setCodes] = useState<CodeRow[]>([]);
  const [units, setUnits] = useState<Opt[]>([]);
  const [cats, setCats] = useState<Opt[]>([]);
  const [taxes, setTaxes] = useState<(Opt & { rate: string; is_active: boolean })[]>([]);
  const [showCost, setShowCost] = useState(true);
  const [loading, setLoading] = useState(!isNew);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const take = (it: Item) => {
    setLoaded(it);
    setF({
      code: it.code, name: it.name, short_name: it.short_name ?? "", category_id: it.category_id ?? "",
      base_uom: it.base_uom, sales_tax_code_id: it.sales_tax_code_id ?? "",
      purchase_tax_code_id: it.purchase_tax_code_id ?? "",
      einvoice_classification: it.einvoice_classification, track_batch: it.track_batch, is_active: it.is_active,
    });
    // cost is left out of the answer for users who may not see it: then it is not on the form
    setShowCost(it.uoms.every((u) => u.purchase_price !== undefined));
    setUoms(it.uoms.map((u) => ({
      key: nextKey++, uom: u.uom, factor: num(u.factor), selling_price: num(u.selling_price),
      purchase_price: num(u.purchase_price),
    })));
    setCodes(it.barcodes.map((b) => ({ key: nextKey++, barcode: b.barcode, uom: b.uom })));
  };

  useEffect(() => {
    api<Page<Opt>>("/uoms").then((p) => {
      setUnits(p.items);
      if (isNew && p.items[0]) {
        const base = p.items.find((u) => u.code === "PCS") ?? p.items[0];
        setF((x) => (x.base_uom ? x : { ...x, base_uom: base.code }));
        setUoms((rows) => rows.length ? rows
          : [{ key: nextKey++, uom: base.code, factor: "1", selling_price: "", purchase_price: "" }]);
      }
    }).catch(() => undefined);
    api<Page<Opt>>("/item-categories").then((p) => setCats(p.items)).catch(() => undefined);
    api<Page<Opt & { rate: string; is_active: boolean }>>("/tax-codes").then((p) => setTaxes(p.items))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (isNew) return;
    setLoading(true);
    api<Item>(`/items/${id}`).then(take).catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
  }, [id, isNew]);

  const set = (patch: Partial<typeof f>) => setF((x) => ({ ...x, ...patch }));
  const setUom = (key: number, patch: Partial<UomRow>) =>
    setUoms((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const setCode = (key: number, patch: Partial<CodeRow>) =>
    setCodes((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const used = new Set(uoms.map((u) => u.uom));
  const free = units.filter((u) => !used.has(u.code));

  /** The base unit always has factor 1: picking another base moves that rule with it. */
  const changeBase = (code: string) => {
    set({ base_uom: code });
    setUoms((rs) => (rs.some((r) => r.uom === code)
      ? rs.map((r) => (r.uom === code ? { ...r, factor: "1" } : r))
      : [{ key: nextKey++, uom: code, factor: "1", selling_price: "", purchase_price: "" }, ...rs]));
  };

  const check = (): string | null => {
    if (!f.code.trim() || /\s/.test(f.code.trim())) return t("itemCodeRule");
    if (!f.name.trim()) return t("name");
    if (!/^\d{3}$/.test(f.einvoice_classification)) return t("classification");
    for (const u of uoms) {
      if (!isNum(u.factor) || Number(u.factor) <= 0) return `${u.uom}: ${t("factor")}`;
      if (u.selling_price.trim() && !isNum(u.selling_price)) return `${u.uom}: ${t("price")}`;
      if (u.purchase_price.trim() && !isNum(u.purchase_price)) return `${u.uom}: ${t("costPrice")}`;
    }
    if (codes.some((c) => !c.barcode.trim())) return t("barcode");
    return null;
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const bad = check();
      if (bad) throw new Error(bad);
      const body = {
        code: f.code.trim(), name: f.name.trim(), short_name: f.short_name.trim() || null,
        category_id: f.category_id || null, base_uom: f.base_uom,
        sales_tax_code_id: f.sales_tax_code_id || null, purchase_tax_code_id: f.purchase_tax_code_id || null,
        einvoice_classification: f.einvoice_classification, track_batch: f.track_batch,
        uoms: uoms.map((u) => ({
          uom: u.uom, factor: u.uom === f.base_uom ? "1" : u.factor.trim(),
          selling_price: u.selling_price.trim() || null,
          // left out for users who cannot see cost: the server then keeps what is there
          ...(showCost ? { purchase_price: u.purchase_price.trim() || null } : {}),
        })),
        barcodes: codes.map((c) => ({ barcode: c.barcode.trim(), uom: c.uom })),
      };
      if (isNew) {
        const made = await api<Item>("/items", { method: "POST", body });
        nav(`/items/edit/${made.id}`, { replace: true });
        take(made);
      } else if (loaded) {
        take(await api<Item>(`/items/${loaded.id}`,
          { method: "PATCH", body: { version: loaded.version, is_active: f.is_active, ...body } }));
      }
      setNotice(t("saved"));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <section className="card glass"><span className="muted">{t("loading")}</span></section>;
  if (loadError || (isNew && !editable)) {
    return (
      <section className="card glass">
        <Link to="/items" className="muted" style={{ textDecoration: "none" }}>← {t("items")}</Link>
        <div className="error" role="alert">{loadError ?? t("noAccess")}</div>
      </section>
    );
  }
  const taxSelect = (id: string, value: string, onChange: (v: string) => void) => (
    <select id={id} className="input" value={value} disabled={!editable} onChange={(e) => onChange(e.target.value)}>
      <option value="">{t("noTax")}</option>
      {taxes.filter((x) => x.is_active || x.id === value).map((x) => (
        <option key={x.id} value={x.id}>{x.code} · {x.name} ({Number(x.rate)}%)</option>
      ))}
    </select>
  );

  return (
    <div className="docform">
      <section className="card glass">
        <div className="row">
          <div>
            <Link to={loaded ? `/items?q=${encodeURIComponent(loaded.code)}` : "/items"} className="muted"
                  style={{ textDecoration: "none" }}>← {t("items")}</Link>
            <h2 style={{ margin: "4px 0 0" }}>{isNew ? t("newItem") : loaded?.name}</h2>
          </div>
          {!editable && <span className="muted nowrap">{t("readOnly")}</span>}
        </div>
        <div className="doc-head">
          <div className="field">
            <label htmlFor="i-code">{t("code")}</label>
            <input id="i-code" className="input" value={f.code} disabled={!editable} maxLength={30}
                   onChange={(e) => set({ code: e.target.value })} />
          </div>
          <div className="field wide">
            <label htmlFor="i-name">{t("name")}</label>
            <input id="i-name" className="input" value={f.name} disabled={!editable} maxLength={200}
                   onChange={(e) => set({ name: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="i-short">{t("shortName")}</label>
            <input id="i-short" className="input" value={f.short_name} disabled={!editable} maxLength={60}
                   onChange={(e) => set({ short_name: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="i-cat">{t("category")}</label>
            <select id="i-cat" className="input" value={f.category_id} disabled={!editable}
                    onChange={(e) => set({ category_id: e.target.value })}>
              <option value="">—</option>
              {cats.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="i-base">{t("baseUnit")}</label>
            <select id="i-base" className="input" value={f.base_uom} disabled={!editable}
                    onChange={(e) => changeBase(e.target.value)}>
              {units.map((u) => <option key={u.id} value={u.code}>{u.code} · {u.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="i-stax">{t("salesTax")}</label>
            {taxSelect("i-stax", f.sales_tax_code_id, (x) => set({ sales_tax_code_id: x }))}
          </div>
          <div className="field">
            <label htmlFor="i-ptax">{t("purchaseTax")}</label>
            {taxSelect("i-ptax", f.purchase_tax_code_id, (x) => set({ purchase_tax_code_id: x }))}
          </div>
          <div className="field">
            <label htmlFor="i-class">{t("classification")}</label>
            <input id="i-class" className="input" value={f.einvoice_classification} disabled={!editable}
                   maxLength={3} inputMode="numeric" onChange={(e) => set({ einvoice_classification: e.target.value })} />
          </div>
        </div>
        {!isNew && editable && (
          <label className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
            <input type="checkbox" checked={f.is_active} onChange={(e) => set({ is_active: e.target.checked })} />
            <span>{t("activeHint")}</span>
          </label>
        )}
      </section>

      <section className="card glass">
        <h3>{t("units")}</h3>
        <span className="muted">{t("unitsHint")}</span>
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead>
              <tr>
                <th>{t("uom")}</th><th className="num">{t("factor")}</th><th className="num">{t("price")}</th>
                {showCost && <th className="num">{t("costPrice")}</th>}{editable && <th />}
              </tr>
            </thead>
            <tbody>
              {uoms.map((u) => {
                const base = u.uom === f.base_uom;
                return (
                  <tr key={u.key}>
                    <td>
                      <select className="input sm" value={u.uom} disabled={!editable || base} aria-label={t("uom")}
                              onChange={(e) => setUom(u.key, { uom: e.target.value })}>
                        {units.filter((x) => x.code === u.uom || !used.has(x.code))
                          .map((x) => <option key={x.id} value={x.code}>{x.code}</option>)}
                      </select>
                      {base && <span className="muted"> {t("baseUnit").toLowerCase()}</span>}
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={base ? "1" : u.factor}
                             disabled={!editable || base} aria-label={`${t("factor")} ${u.uom}`}
                             onChange={(e) => setUom(u.key, { factor: e.target.value })} />
                    </td>
                    <td className="num">
                      <input className="input sm num" inputMode="decimal" value={u.selling_price} disabled={!editable}
                             aria-label={`${t("price")} ${u.uom}`}
                             onChange={(e) => setUom(u.key, { selling_price: e.target.value })} />
                    </td>
                    {showCost && (
                      <td className="num">
                        <input className="input sm num" inputMode="decimal" value={u.purchase_price}
                               disabled={!editable} aria-label={`${t("costPrice")} ${u.uom}`}
                               onChange={(e) => setUom(u.key, { purchase_price: e.target.value })} />
                      </td>
                    )}
                    {editable && (
                      <td>
                        {!base && (
                          <button className="chip-btn" aria-label={`${t("remove")} ${u.uom}`}
                                  onClick={() => {
                                    setUoms((rs) => rs.filter((r) => r.key !== u.key));
                                    setCodes((rs) => rs.filter((c) => c.uom !== u.uom));
                                  }}>✕</button>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {editable && free.length > 0 && (
          <button className="chip-btn" style={{ alignSelf: "flex-start" }}
                  onClick={() => setUoms((rs) => [...rs, {
                    key: nextKey++, uom: free[0].code, factor: "", selling_price: "", purchase_price: "" }])}>
            + {t("addUnit")}
          </button>
        )}
      </section>

      <section className="card glass">
        <h3>{t("barcodes")}</h3>
        <span className="muted">{t("barcodesHint")}</span>
        {codes.length === 0 && <span className="muted">{t("noBarcodes")}</span>}
        {codes.map((c) => (
          <div key={c.key} className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
            <input className="input" style={{ flex: 1, maxWidth: 280 }} value={c.barcode} disabled={!editable}
                   maxLength={30} inputMode="numeric" aria-label={t("barcode")}
                   onChange={(e) => setCode(c.key, { barcode: e.target.value })} />
            <select className="input sm" value={c.uom} disabled={!editable} aria-label={t("uom")}
                    onChange={(e) => setCode(c.key, { uom: e.target.value })}>
              {uoms.map((u) => <option key={u.key}>{u.uom}</option>)}
            </select>
            {editable && (
              <button className="chip-btn" aria-label={t("remove")}
                      onClick={() => setCodes((rs) => rs.filter((r) => r.key !== c.key))}>✕</button>
            )}
          </div>
        ))}
        {editable && (
          <button className="chip-btn" style={{ alignSelf: "flex-start" }}
                  onClick={() => setCodes((rs) => [...rs, { key: nextKey++, barcode: "", uom: f.base_uom }])}>
            + {t("addBarcode")}
          </button>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {notice && !error && <div className="muted" role="status">{notice}</div>}
        {editable && (
          <div className="actions">
            <button className="btn" disabled={busy} onClick={save}>{t(isNew ? "create" : "update")}</button>
          </div>
        )}
      </section>
    </div>
  );
}
