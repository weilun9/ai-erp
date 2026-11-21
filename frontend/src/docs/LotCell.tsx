import { useState } from "react";
import { useSession } from "../session";

// Batch / serial numbers of one document line (items that track them). Quantities are in
// the item's base unit. Incoming lines (GRN, returns) must list them; outgoing lines may
// leave them empty - the server then takes the earliest-expiring stock first.

export interface Lot { lot_no: string; expiry_date: string | null; qty: string }

const fmt = (q: string) => Number(q).toLocaleString("en-MY", { maximumFractionDigits: 4 });

export function LotCell({ lots, serial, need, baseUom, editable, incoming, moved, onChange }: {
  lots: Lot[];
  serial: boolean;
  need: number;               // line quantity in base units
  baseUom: string;
  editable: boolean;
  incoming: boolean;
  moved?: Lot[];              // posted: what the stock actually moved (incl. auto-picked)
  onChange: (lots: Lot[]) => void;
}) {
  const { t } = useSession();
  const [no, setNo] = useState("");
  const [exp, setExp] = useState("");
  const [qty, setQty] = useState("");
  const have = lots.reduce((s, x) => s + Number(x.qty), 0);
  const left = Math.max(0, need - have);
  const shown = moved && moved.length ? moved : lots;

  const add = () => {
    if (serial) {
      // a scanner or a paste can bring several serial numbers at once
      const fresh = no.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean)
        .filter((x) => !lots.some((l) => l.lot_no === x));
      if (!fresh.length) return;
      onChange([...lots, ...fresh.map((x) => ({ lot_no: x, expiry_date: null, qty: "1" }))]);
    } else {
      const q = qty.trim() || String(left);
      if (!no.trim() || !(Number(q) > 0)) return;
      onChange([...lots, { lot_no: no.trim(), expiry_date: exp || null, qty: q }]);
      setExp("");
      setQty("");
    }
    setNo("");
  };

  const mismatch = lots.length > 0 && Math.abs(have - need) > 1e-9;
  return (
    <div className="lot-cell">
      {shown.length > 0 && (
        <div className="lot-chips">
          {shown.map((l, i) => (
            <span key={l.lot_no + i} className="lot-chip">
              {serial ? "SN" : t("lotBatch")} {l.lot_no}
              {l.expiry_date && <span className="muted"> · {t("lotExp")} {l.expiry_date}</span>}
              {!serial && <b> × {fmt(l.qty)}</b>}
              {editable && !moved?.length && (
                <button type="button" aria-label={t("remove")}
                        onClick={() => onChange(lots.filter((_, j) => j !== i))}>✕</button>
              )}
            </span>
          ))}
        </div>
      )}
      {editable && (
        <form className="lot-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
          <input className="input sm" value={no} placeholder={serial ? t("lotSerialPh") : t("lotNoPh")}
                 aria-label={serial ? t("lotSerial") : t("lotBatch")} onChange={(e) => setNo(e.target.value)} />
          {!serial && (
            <>
              <input className="input sm" type="date" value={exp} aria-label={t("lotExpiry")}
                     title={t("lotExpiry")} onChange={(e) => setExp(e.target.value)} />
              <input className="input sm num" inputMode="decimal" value={qty} placeholder={fmt(String(left))}
                     aria-label={`${t("qtyLabel")} (${baseUom})`} onChange={(e) => setQty(e.target.value)} />
            </>
          )}
          <button className="chip-btn" type="submit">+</button>
        </form>
      )}
      <span className={`muted small${mismatch ? " lot-warn" : ""}`}>
        {lots.length === 0 && !moved?.length
          ? (incoming ? t("lotNeeded") : t("lotAuto"))
          : `${fmt(String(have || shown.reduce((s, x) => s + Number(x.qty), 0)))} / ${fmt(String(need))} ${baseUom}`}
      </span>
    </div>
  );
}
