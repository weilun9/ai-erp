import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, money, qty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import type { Key } from "../i18n";
import { errorText, isNum } from "../docs/shared";

// Create or edit an item - DynaMod's "Stock Maintenance": identity and grouping, tax codes,
// the units it is bought and sold in (factor to the base unit, prices 1-5 and cost), barcodes,
// alert / reorder levels with per-store overrides, notes, and (read-only) stock per store,
// who supplied it and the recent purchase prices.

const LEVELS = ["price_2", "price_3", "price_4", "price_5"] as const;
type Level = (typeof LEVELS)[number];
const KINDS = ["BRAND", "GROUP", "COLOR", "SIZE", "UDF1", "UDF2", "UDF3"] as const;
const KIND_FIELD: Record<(typeof KINDS)[number], string> = {
  BRAND: "brand_id", GROUP: "group_id", COLOR: "color_id", SIZE: "size_id",
  UDF1: "udf1_id", UDF2: "udf2_id", UDF3: "udf3_id",
};
const REORDER = ["min_qty", "max_qty", "reorder_level", "reorder_qty"] as const;
type ReorderKey = (typeof REORDER)[number];
const REORDER_LABEL: Record<ReorderKey, Key> = {
  min_qty: "minQty", max_qty: "maxQty", reorder_level: "reorderLevel", reorder_qty: "reorderQty",
};

type ItemUom = { uom: string; factor: string; selling_price: string | null; purchase_price?: string | null }
  & Partial<Record<Level, string | null>>;
interface Item {
  id: string; code: string; name: string; short_name: string | null; category_id: string | null;
  base_uom: string; purchase_tax_code_id: string | null; sales_tax_code_id: string | null;
  einvoice_classification: string; track_batch: boolean; is_active: boolean; version: number;
  description_2: string | null; article_no: string | null; sales_note: string | null;
  purchase_note: string | null; min_selling_price: string | null;
  [k: string]: unknown;
  uoms: ItemUom[]; barcodes: { barcode: string; uom: string }[];
}
interface Opt { id: string; code: string; name: string }
interface Attr extends Opt { kind: string; is_active: boolean }
type UomRow = { key: number; uom: string; factor: string; selling_price: string; purchase_price: string }
  & Record<Level, string>;
interface CodeRow { key: number; barcode: string; uom: string }
type ReorderSet = Record<ReorderKey, string | null>;
interface LocRow {
  location_id: string; location_code: string; location_name: string; qty: string; avg_cost?: string | null;
  own: ReorderSet; effective: ReorderSet; shelf: string | null; below_reorder: boolean; on_order: string;
}
interface Overview {
  locations: LocRow[]; total_qty: string; total_on_order: string; cost_hidden: boolean;
  suppliers: { supplier_id: string; supplier_code: string; supplier_name: string; last_date: string;
    last_doc_no: string | null; documents: number; base_qty: string; last_unit_cost?: string | null }[];
  purchase_history: { doc_id: string; doc_no: string | null; doc_date: string; supplier_code: string;
    uom: string; qty: string; unit_price: string; discount_pct: string; unit_cost: string }[];
}
type LocEdit = Record<ReorderKey | "shelf", string>;

let nextKey = 1;
const num = (v: unknown) => (v === null || v === undefined ? "" : String(Number(v)));
const blankUom = (uom: string, factor: string): UomRow => ({
  key: nextKey++, uom, factor, selling_price: "", purchase_price: "",
  price_2: "", price_3: "", price_4: "", price_5: "",
});
const EMPTY = {
  code: "", name: "", short_name: "", category_id: "", base_uom: "", sales_tax_code_id: "",
  purchase_tax_code_id: "", einvoice_classification: "022", track_batch: false, is_active: true,
  description_2: "", article_no: "", sales_note: "", purchase_note: "", min_selling_price: "",
  min_qty: "", max_qty: "", reorder_level: "", reorder_qty: "",
  brand_id: "", group_id: "", color_id: "", size_id: "", udf1_id: "", udf2_id: "", udf3_id: "",
};
type Form = typeof EMPTY;

export function ItemForm() {
  const { id } = useParams();
  const { t, can } = useSession();
  const nav = useNavigate();
  const isNew = id === "new";
  const editable = can("md.item.manage");

  const [loaded, setLoaded] = useState<Item | null>(null);
  const [f, setF] = useState<Form>(EMPTY);
  const [uoms, setUoms] = useState<UomRow[]>([]);
  const [codes, setCodes] = useState<CodeRow[]>([]);
  const [units, setUnits] = useState<Opt[]>([]);
  const [cats, setCats] = useState<Opt[]>([]);
  const [attrs, setAttrs] = useState<Attr[]>([]);
  const [taxes, setTaxes] = useState<(Opt & { rate: string; is_active: boolean })[]>([]);
  const [showCost, setShowCost] = useState(true);
  const [loading, setLoading] = useState(!isNew);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [ov, setOv] = useState<Overview | null>(null);
  const [locEdit, setLocEdit] = useState<Record<string, LocEdit>>({});
  const [locBusy, setLocBusy] = useState(false);
  const [locMsg, setLocMsg] = useState<{ error?: string; notice?: string }>({});

  const take = (it: Item) => {
    setLoaded(it);
    const s = (k: string) => (it[k] === null || it[k] === undefined ? "" : String(it[k]));
    setF({
      code: it.code, name: it.name, short_name: s("short_name"), category_id: s("category_id"),
      base_uom: it.base_uom, sales_tax_code_id: s("sales_tax_code_id"),
      purchase_tax_code_id: s("purchase_tax_code_id"),
      einvoice_classification: it.einvoice_classification, track_batch: it.track_batch, is_active: it.is_active,
      description_2: s("description_2"), article_no: s("article_no"), sales_note: s("sales_note"),
      purchase_note: s("purchase_note"), min_selling_price: num(it.min_selling_price),
      min_qty: num(it.min_qty), max_qty: num(it.max_qty), reorder_level: num(it.reorder_level),
      reorder_qty: num(it.reorder_qty),
      brand_id: s("brand_id"), group_id: s("group_id"), color_id: s("color_id"), size_id: s("size_id"),
      udf1_id: s("udf1_id"), udf2_id: s("udf2_id"), udf3_id: s("udf3_id"),
    });
    // cost is left out of the answer for users who may not see it: then it is not on the form
    setShowCost(it.uoms.every((u) => u.purchase_price !== undefined));
    setUoms(it.uoms.map((u) => ({
      key: nextKey++, uom: u.uom, factor: num(u.factor), selling_price: num(u.selling_price),
      purchase_price: num(u.purchase_price),
      price_2: num(u.price_2), price_3: num(u.price_3), price_4: num(u.price_4), price_5: num(u.price_5),
    })));
    setCodes(it.barcodes.map((b) => ({ key: nextKey++, barcode: b.barcode, uom: b.uom })));
  };
  const takeOv = (o: Overview) => {
    setOv(o);
    setLocEdit(Object.fromEntries(o.locations.map((r) => [r.location_id, {
      min_qty: num(r.own.min_qty), max_qty: num(r.own.max_qty), reorder_level: num(r.own.reorder_level),
      reorder_qty: num(r.own.reorder_qty), shelf: r.shelf ?? "",
    }])));
  };

  useEffect(() => {
    api<Page<Opt>>("/uoms").then((p) => {
      setUnits(p.items);
      if (isNew && p.items[0]) {
        const base = p.items.find((u) => u.code === "PCS") ?? p.items[0];
        setF((x) => (x.base_uom ? x : { ...x, base_uom: base.code }));
        setUoms((rows) => rows.length ? rows : [blankUom(base.code, "1")]);
      }
    }).catch(() => undefined);
    api<Page<Opt>>("/item-categories").then((p) => setCats(p.items)).catch(() => undefined);
    api<Page<Attr>>("/item-attributes").then((p) => setAttrs(p.items)).catch(() => undefined);
    api<Page<Opt & { rate: string; is_active: boolean }>>("/tax-codes").then((p) => setTaxes(p.items))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (isNew) return;
    setLoading(true);
    api<Item>(`/items/${id}`).then(take).catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
    api<Overview>(`/items/${id}/overview`).then(takeOv).catch(() => setOv(null));
  }, [id, isNew]);

  const set = (patch: Partial<Form>) => setF((x) => ({ ...x, ...patch }));
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
      : [blankUom(code, "1"), ...rs]));
  };

  const check = (): string | null => {
    if (!f.code.trim() || /\s/.test(f.code.trim())) return t("itemCodeRule");
    if (!f.name.trim()) return t("name");
    if (!/^\d{3}$/.test(f.einvoice_classification)) return t("classification");
    for (const u of uoms) {
      if (!isNum(u.factor) || Number(u.factor) <= 0) return `${u.uom}: ${t("factor")}`;
      if (u.selling_price.trim() && !isNum(u.selling_price)) return `${u.uom}: ${t("price")}`;
      for (const [i, l] of LEVELS.entries()) {
        if (u[l].trim() && !isNum(u[l])) return `${u.uom}: ${t("priceN").replace("{n}", String(i + 2))}`;
      }
      if (u.purchase_price.trim() && !isNum(u.purchase_price)) return `${u.uom}: ${t("costPrice")}`;
    }
    for (const k of [...REORDER, "min_selling_price"] as const) {
      if (f[k].trim() && (!isNum(f[k]) || Number(f[k]) < 0)) {
        return k === "min_selling_price" ? t("minSellPrice") : t(REORDER_LABEL[k]);
      }
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
      const orNull = (x: string) => x.trim() || null;
      const body = {
        code: f.code.trim(), name: f.name.trim(), short_name: orNull(f.short_name),
        category_id: f.category_id || null, base_uom: f.base_uom,
        sales_tax_code_id: f.sales_tax_code_id || null, purchase_tax_code_id: f.purchase_tax_code_id || null,
        einvoice_classification: f.einvoice_classification, track_batch: f.track_batch,
        description_2: orNull(f.description_2), article_no: orNull(f.article_no),
        sales_note: orNull(f.sales_note), purchase_note: orNull(f.purchase_note),
        min_selling_price: orNull(f.min_selling_price),
        ...Object.fromEntries(REORDER.map((k) => [k, orNull(f[k])])),
        ...Object.fromEntries(KINDS.map((k) => [KIND_FIELD[k], f[KIND_FIELD[k] as keyof Form] || null])),
        uoms: uoms.map((u) => ({
          uom: u.uom, factor: u.uom === f.base_uom ? "1" : u.factor.trim(),
          selling_price: orNull(u.selling_price),
          ...Object.fromEntries(LEVELS.map((l) => [l, orNull(u[l])])),
          // left out for users who cannot see cost: the server then keeps what is there
          ...(showCost ? { purchase_price: orNull(u.purchase_price) } : {}),
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
        api<Overview>(`/items/${loaded.id}/overview`).then(takeOv).catch(() => undefined);
      }
      setNotice(t("saved"));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const saveStores = async () => {
    if (!loaded || !ov) return;
    setLocBusy(true);
    setLocMsg({});
    try {
      const rows = ov.locations.map((r) => {
        const e = locEdit[r.location_id];
        for (const k of REORDER) {
          if (e[k].trim() && (!isNum(e[k]) || Number(e[k]) < 0)) throw new Error(`${r.location_code}: ${t(REORDER_LABEL[k])}`);
        }
        return {
          location_id: r.location_id, shelf: e.shelf.trim() || null,
          ...Object.fromEntries(REORDER.map((k) => [k, e[k].trim() || null])),
        };
      });
      takeOv(await api<Overview>(`/items/${loaded.id}/locations`, { method: "PUT", body: rows }));
      setLocMsg({ notice: t("saved") });
    } catch (e) {
      setLocMsg({ error: errorText(e) });
    } finally {
      setLocBusy(false);
    }
  };
  const copyToStores = async () => {
    if (!loaded || !window.confirm(t("copyConfirm"))) return;
    setLocBusy(true);
    setLocMsg({});
    try {
      const r = await api<{ updated: number }>("/items/copy-reorder-to-locations", {
        method: "POST", body: { item_id: loaded.id, fields: [...REORDER] },
      });
      takeOv(await api<Overview>(`/items/${loaded.id}/overview`));
      setLocMsg({ notice: t("copied").replace("{n}", String(r.updated)) });
    } catch (e) {
      setLocMsg({ error: errorText(e) });
    } finally {
      setLocBusy(false);
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
  const text = (key: keyof Form, label: Key, max: number, wide = false, numeric = false) => (
    <div className={`field${wide ? " wide" : ""}`} key={key}>
      <label htmlFor={`i-${key}`}>{t(label)}</label>
      <input id={`i-${key}`} className={`input${numeric ? " num" : ""}`} value={String(f[key])} disabled={!editable}
             maxLength={max} inputMode={numeric ? "decimal" : undefined}
             onChange={(e) => set({ [key]: e.target.value } as Partial<Form>)} />
    </div>
  );
  const kindsInUse = KINDS.filter((k) => attrs.some((a) => a.kind === k) || f[KIND_FIELD[k] as keyof Form]);

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
          {text("code", "code", 30)}
          {text("name", "name", 200, true)}
          {text("description_2", "description2", 200, true)}
          {text("short_name", "shortName", 60)}
          {text("article_no", "articleNo", 50)}
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
        <div className="row">
          <h3 style={{ margin: 0 }}>{t("grouping")}</h3>
          {can("md.setup.read") && (
            <Link className="chip-btn" to="/settings?tab=groups">{t("setItemGroups")}</Link>
          )}
        </div>
        {kindsInUse.length === 0 ? <span className="muted">{t("noResults")}</span> : (
          <div className="doc-head">
            {kindsInUse.map((k) => {
              const field = KIND_FIELD[k] as keyof Form;
              const value = String(f[field]);
              return (
                <div className="field" key={k}>
                  <label htmlFor={`i-${k}`}>{t(`attr${k}` as Key)}</label>
                  <select id={`i-${k}`} className="input" value={value} disabled={!editable}
                          onChange={(e) => set({ [field]: e.target.value } as Partial<Form>)}>
                    <option value="">—</option>
                    {attrs.filter((a) => a.kind === k && (a.is_active || a.id === value)).map((a) => (
                      <option key={a.id} value={a.id}>{a.code} · {a.name}</option>
                    ))}
                  </select>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="card glass">
        <h3>{t("units")}</h3>
        <span className="muted">{t("unitsHint")} {t("priceLevelsHint")}</span>
        <div className="scroll-x">
          <table className="table doc-lines">
            <thead>
              <tr>
                <th>{t("uom")}</th><th className="num">{t("factor")}</th>
                <th className="num">{t("priceN").replace("{n}", "1")}</th>
                {LEVELS.map((l, i) => <th key={l} className="num">{t("priceN").replace("{n}", String(i + 2))}</th>)}
                {showCost && <th className="num">{t("costPrice")}</th>}{editable && <th />}
              </tr>
            </thead>
            <tbody>
              {uoms.map((u) => {
                const base = u.uom === f.base_uom;
                const priceCell = (k: "selling_price" | Level, label: string) => (
                  <td className="num" key={k}>
                    <input className="input sm num" inputMode="decimal" value={u[k]} disabled={!editable}
                           aria-label={`${label} ${u.uom}`} style={{ minWidth: 70 }}
                           onChange={(e) => setUom(u.key, { [k]: e.target.value } as Partial<UomRow>)} />
                  </td>
                );
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
                    {priceCell("selling_price", t("priceN").replace("{n}", "1"))}
                    {LEVELS.map((l, i) => priceCell(l, t("priceN").replace("{n}", String(i + 2))))}
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
                  onClick={() => setUoms((rs) => [...rs, blankUom(free[0].code, "")])}>
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
      </section>

      <section className="card glass">
        <h3>{t("stockReorder")}</h3>
        <span className="muted">{t("stockReorderHint")}</span>
        <div className="doc-head">
          {REORDER.map((k) => text(k, REORDER_LABEL[k], 20, false, true))}
          {text("min_selling_price", "minSellPrice", 20, false, true)}
        </div>
      </section>

      <section className="card glass">
        <h3>{t("itemNotes")}</h3>
        <div className="doc-head">
          {(["sales_note", "purchase_note"] as const).map((k) => (
            <div className="field wide" key={k}>
              <label htmlFor={`i-${k}`}>{t(k === "sales_note" ? "salesNote" : "purchaseNote")}</label>
              <textarea id={`i-${k}`} className="input" rows={3} value={f[k]} disabled={!editable} maxLength={2000}
                        onChange={(e) => set({ [k]: e.target.value } as Partial<Form>)} />
            </div>
          ))}
        </div>
        {error && <div className="error" role="alert">{error}</div>}
        {notice && !error && <div className="muted" role="status">{notice}</div>}
        {editable && (
          <div className="actions">
            <button className="btn" disabled={busy} onClick={save}>{t(isNew ? "create" : "update")}</button>
          </div>
        )}
      </section>

      {isNew ? (
        <section className="card glass"><span className="muted">{t("saveItemFirst")}</span></section>
      ) : ov && (
        <>
          <section className="card glass">
            <div className="row">
              <h3 style={{ margin: 0 }}>{t("perStore")}</h3>
              <span className="muted">
                {t("totalOnHand")}: <b>{qty(ov.total_qty)} {f.base_uom}</b>
                {Number(ov.total_on_order) > 0 && <> · {t("onOrder")}: <b>{qty(ov.total_on_order)}</b></>}
              </span>
            </div>
            {editable && <span className="muted">{t("perStoreHint")}</span>}
            <div className="scroll-x">
              <table className="table doc-lines">
                <thead>
                  <tr>
                    <th>{t("store")}</th><th className="num">{t("onHand")}</th><th className="num">{t("onOrder")}</th>
                    {!ov.cost_hidden && <th className="num">{t("avgCost")}</th>}
                    {REORDER.map((k) => <th key={k} className="num">{t(REORDER_LABEL[k])}</th>)}
                    <th>{t("shelf")}</th>
                  </tr>
                </thead>
                <tbody>
                  {ov.locations.map((r) => {
                    const e = locEdit[r.location_id];
                    return (
                      <tr key={r.location_id}>
                        <td className="nowrap">{r.location_code}</td>
                        <td className="num nowrap">
                          <span className={`badge ${Number(r.qty) < 0 ? "red" : r.below_reorder ? "amber" : "green"}`}>
                            {qty(r.qty)}
                          </span>
                          {r.below_reorder && <span className="muted"> {t("belowReorder")}</span>}
                        </td>
                        <td className="num">{Number(r.on_order) ? qty(r.on_order) : "—"}</td>
                        {!ov.cost_hidden && <td className="num">{r.avg_cost != null ? money(r.avg_cost) : "—"}</td>}
                        {REORDER.map((k) => (
                          <td className="num" key={k}>
                            {editable && e ? (
                              <input className="input sm num" inputMode="decimal" value={e[k]} style={{ minWidth: 64 }}
                                     placeholder={num(r.effective[k]) || "—"}
                                     aria-label={`${r.location_code} ${t(REORDER_LABEL[k])}`}
                                     onChange={(ev) => setLocEdit((m) => ({ ...m, [r.location_id]: { ...e, [k]: ev.target.value } }))} />
                            ) : (num(r.effective[k]) || "—")}
                          </td>
                        ))}
                        <td>
                          {editable && e ? (
                            <input className="input sm" value={e.shelf} maxLength={30} style={{ minWidth: 70 }}
                                   aria-label={`${r.location_code} ${t("shelf")}`}
                                   onChange={(ev) => setLocEdit((m) => ({ ...m, [r.location_id]: { ...e, shelf: ev.target.value } }))} />
                          ) : (r.shelf ?? "—")}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {locMsg.error && <div className="error" role="alert">{locMsg.error}</div>}
            {locMsg.notice && <div className="muted" role="status">{locMsg.notice}</div>}
            {editable && (
              <div className="actions">
                <button className="chip-btn" disabled={locBusy} onClick={copyToStores}>{t("copyToStores")}</button>
                <button className="btn" disabled={locBusy} onClick={saveStores}>{t("saveStores")}</button>
              </div>
            )}
          </section>

          <section className="card glass">
            <h3>{t("itemSuppliers")}</h3>
            {ov.suppliers.length === 0 ? <span className="muted">{t("noResults")}</span> : (
              <div className="scroll-x">
                <table className="table doc-lines">
                  <thead>
                    <tr>
                      <th>{t("supplier")}</th><th>{t("lastDelivery")}</th>
                      <th className="num">{t("deliveries")}</th><th className="num">{t("qtyBase")}</th>
                      {!ov.cost_hidden && <th className="num">{t("lastCost")}</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {ov.suppliers.map((s) => (
                      <tr key={s.supplier_id}>
                        <td><Link to={`/parties/supplier/${s.supplier_id}`}>{s.supplier_code}</Link> {s.supplier_name}</td>
                        <td className="nowrap">{s.last_date} · {s.last_doc_no}</td>
                        <td className="num">{s.documents}</td>
                        <td className="num">{qty(s.base_qty)}</td>
                        {!ov.cost_hidden && <td className="num">{s.last_unit_cost != null ? money(s.last_unit_cost) : "—"}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {!ov.cost_hidden && (
            <section className="card glass">
              <h3>{t("purchaseHistory")}</h3>
              {ov.purchase_history.length === 0 ? <span className="muted">{t("noResults")}</span> : (
                <div className="scroll-x">
                  <table className="table doc-lines">
                    <thead>
                      <tr>
                        <th>{t("date")}</th><th>{t("docNo")}</th><th>{t("supplier")}</th>
                        <th className="num">{t("qtyLabel")}</th><th className="num">{t("unitPrice")}</th>
                        <th className="num">{t("discPct")}</th><th className="num">{t("lastCost")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ov.purchase_history.map((r, i) => (
                        <tr key={`${r.doc_id}-${i}`}>
                          <td className="nowrap">{r.doc_date}</td>
                          <td><Link to={`/docs/grn/${r.doc_id}`}>{r.doc_no}</Link></td>
                          <td>{r.supplier_code}</td>
                          <td className="num nowrap">{qty(r.qty)} {r.uom}</td>
                          <td className="num">{money(r.unit_price)}</td>
                          <td className="num">{Number(r.discount_pct) ? qty(r.discount_pct) : "—"}</td>
                          <td className="num">{money(r.unit_cost)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
