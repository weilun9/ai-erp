import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import JsBarcode from "jsbarcode";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";

// Barcode labels: pick items (or arrive with ?items=id,id), choose the unit/barcode and how
// many copies, pick a paper layout, print. EAN-13 / UPC-A / EAN-8 barcodes are drawn as such
// (they scan on any till); anything else is printed as Code 128.

interface Item {
  id: string;
  code: string;
  name: string;
  base_uom: string;
  uoms: { uom: string; selling_price: string | null }[];
  barcodes: { barcode: string; uom: string }[];
}
interface Row { item: Item; barcode: string; uom: string; copies: number }

type LayoutId = "a4-24" | "a4-40" | "roll-50x30";
const LAYOUTS: Record<LayoutId, { cols: number; rows: number; w: number; h: number; page: string }> = {
  "a4-24": { cols: 3, rows: 8, w: 70, h: 37.125, page: "A4" },
  "a4-40": { cols: 4, rows: 10, w: 52.5, h: 29.7, page: "A4" },
  "roll-50x30": { cols: 1, rows: 1, w: 50, h: 30, page: "50mm 30mm" },
};

function format(code: string): string {
  if (/^\d{13}$/.test(code)) return "EAN13";
  if (/^\d{12}$/.test(code)) return "UPC";
  if (/^\d{8}$/.test(code)) return "EAN8";
  return "CODE128";
}

/** One barcode as a scalable SVG; a bad check digit falls back to Code 128 (still scans). */
function Barcode({ value, small }: { value: string; small: boolean }) {
  const ref = useRef<SVGSVGElement>(null);
  useLayoutEffect(() => {
    const svg = ref.current;
    if (!svg) return;
    const opts = { height: small ? 26 : 36, width: 2, margin: 0, fontSize: small ? 13 : 15,
                   textMargin: 1, background: "transparent", lineColor: "#000" };
    try {
      JsBarcode(svg, value, { ...opts, format: format(value) });
    } catch {
      JsBarcode(svg, value, { ...opts, format: "CODE128" });
    }
    const w = svg.getAttribute("width")?.replace("px", "");
    const h = svg.getAttribute("height")?.replace("px", "");
    if (w && h) {
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      svg.removeAttribute("width");
      svg.removeAttribute("height");
    }
  }, [value, small]);
  return <svg ref={ref} className="label-code" preserveAspectRatio="xMidYMid meet" />;
}

const priceOf = (item: Item, uom: string) =>
  item.uoms.find((u) => u.uom === uom)?.selling_price ?? null;

function newRow(item: Item): Row {
  const b = item.barcodes.find((x) => x.uom === item.base_uom) ?? item.barcodes[0];
  return { item, barcode: b?.barcode ?? item.code, uom: b?.uom ?? item.base_uom, copies: 1 };
}

export function Labels() {
  const { t } = useSession();
  const [params] = useSearchParams();
  const [rows, setRows] = useState<Row[]>([]);
  const [layout, setLayout] = useState<LayoutId>("a4-24");
  const [showPrice, setShowPrice] = useState(true);
  const [skip, setSkip] = useState(0);  // used labels at the top of a part-used A4 sheet

  // ?items=id,id (from the Items page or a goods-received note)
  useEffect(() => {
    const ids = (params.get("items") ?? "").split(",").filter(Boolean);
    if (!ids.length) return;
    Promise.all(ids.map((id) => api<Item>(`/items/${id}`).catch(() => null))).then((items) =>
      setRows(items.filter((x): x is Item => !!x).map(newRow)));
  }, [params]);

  // the roll printer needs its own paper size while this page is open
  useEffect(() => {
    const el = document.createElement("style");
    el.textContent = `@media print { @page { size: ${LAYOUTS[layout].page}; margin: 0; } }`;
    document.head.appendChild(el);
    return () => el.remove();
  }, [layout]);

  const L = LAYOUTS[layout];
  const per = L.cols * L.rows;
  const labels: (Row | null)[] = [
    ...Array(layout === "roll-50x30" ? 0 : Math.min(skip, per - 1)).fill(null),
    ...rows.flatMap((r) => Array(Math.max(0, r.copies)).fill(r)),
  ];
  const pages: (Row | null)[][] = [];
  for (let i = 0; i < labels.length; i += per) pages.push(labels.slice(i, i + per));
  const last = pages[pages.length - 1];
  const empty = last ? per - last.length : 0;  // the rest of the sheet, shown empty
  const count = labels.filter(Boolean).length;
  const set = (i: number, patch: Partial<Row>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="labels-page">
      <section className="card glass no-print">
        <h2 style={{ margin: 0 }}>{t("labelsTitle")}</h2>
        <span className="muted">{t("labelsHint")}</span>
        <Picker<Item>
          placeholder={t("labelsAdd")}
          search={(q) => api<Page<Item>>("/items", { query: { q, page_size: 8 } }).then((p) => p.items)}
          label={(i) => `${i.code} · ${i.name}`}
          onPick={(i) => setRows([...rows, newRow(i)])}
          onEnter={async (text) => {
            const hit = (await api<Page<Item>>("/items", { query: { q: text, page_size: 2 } })).items;
            if (hit.length !== 1) return false;
            setRows([...rows, newRow(hit[0])]);
            return true;
          }}
        />
        {rows.length > 0 && (
          <div className="scroll-x">
            <table className="table one-line">
              <thead><tr><th>{t("item")}</th><th>{t("labelsBarcode")}</th><th className="num">{t("price")}</th>
                <th className="num">{t("labelsCopies")}</th><th /></tr></thead>
              <tbody>
                {rows.map((r, i) => {
                  const choices = r.item.barcodes.length ? r.item.barcodes
                    : [{ barcode: r.item.code, uom: r.item.base_uom }];
                  return (
                    <tr key={i}>
                      <td>{r.item.name}<div className="muted small">{r.item.code}</div></td>
                      <td>
                        <select className="input" value={`${r.barcode}|${r.uom}`} aria-label={t("labelsBarcode")}
                                onChange={(e) => { const [barcode, uom] = e.target.value.split("|"); set(i, { barcode, uom }); }}>
                          {choices.map((b) => (
                            <option key={b.barcode + b.uom} value={`${b.barcode}|${b.uom}`}>{b.barcode} ({b.uom})</option>
                          ))}
                        </select>
                      </td>
                      <td className="num">{money(priceOf(r.item, r.uom))}</td>
                      <td className="num">
                        <input className="input copies" type="number" min={0} max={999} value={r.copies}
                               aria-label={t("labelsCopies")}
                               onChange={(e) => set(i, { copies: Math.min(999, Math.max(0, Number(e.target.value) || 0)) })} />
                      </td>
                      <td className="num">
                        <button className="chip-btn" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                          {t("remove")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="doc-head">
          <div className="field">
            <label htmlFor="lb-layout">{t("labelsPaper")}</label>
            <select id="lb-layout" className="input" value={layout} onChange={(e) => setLayout(e.target.value as LayoutId)}>
              <option value="a4-24">{t("labelsA4x24")}</option>
              <option value="a4-40">{t("labelsA4x40")}</option>
              <option value="roll-50x30">{t("labelsRoll")}</option>
            </select>
          </div>
          {layout !== "roll-50x30" && (
            <div className="field">
              <label htmlFor="lb-skip">{t("labelsSkip")}</label>
              <input id="lb-skip" className="input" type="number" min={0} max={per - 1} value={skip}
                     onChange={(e) => setSkip(Math.min(per - 1, Math.max(0, Number(e.target.value) || 0)))} />
            </div>
          )}
          <label className="check-row">
            <input type="checkbox" checked={showPrice} onChange={(e) => setShowPrice(e.target.checked)} />
            {t("labelsShowPrice")}
          </label>
        </div>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted">{t("labelsCount").replace("{n}", String(count)).replace("{pages}", String(pages.length))}</span>
          <button className="btn" disabled={!count} onClick={() => window.print()}>{t("labelsPrint")}</button>
        </div>
      </section>

      {count > 0 && (
        <section className="card glass label-preview-card">
          <span className="muted no-print">{t("labelsPreview")}</span>
          <div className="label-preview">
            {pages.map((pg, n) => (
              <div key={n} className={`label-sheet ${layout}`}
                   style={{
                     gridTemplateColumns: `repeat(${L.cols}, ${L.w}mm)`,
                     gridAutoRows: `${L.h}mm`,
                     width: `${L.cols * L.w}mm`,
                   }}>
                {pg.map((r, i) => r === null ? <div key={i} className="label blank" /> : (
                  <div key={i} className="label">
                    <div className="label-name">{r.item.name}</div>
                    {showPrice && priceOf(r.item, r.uom) && (
                      <div className="label-price">{money(priceOf(r.item, r.uom))}
                        {r.uom !== r.item.base_uom && <small> / {r.uom}</small>}</div>
                    )}
                    <Barcode value={r.barcode} small={layout !== "a4-24"} />
                  </div>
                ))}
                {n === pages.length - 1 && Array.from({ length: empty }, (_, i) => (
                  <div key={`e${i}`} className="label empty" aria-hidden="true" />
                ))}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
