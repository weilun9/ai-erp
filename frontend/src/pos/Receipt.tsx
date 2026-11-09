import { money, qty } from "../api";

export interface ReceiptData {
  kind: "SALE" | "RETURN";
  receipt_no: string;
  sold_at: string;
  lines: { name: string; uom: string; qty: string; unit_price: string; line_total: string }[];
  subtotal: string;
  tax_total: string;
  rounding: string;
  total: string;
  tendered: string;
  change: string;
  payments: { method: string; amount: string }[];
  offline?: boolean;
  cashier: string;
}

/** 80 mm till receipt; the print stylesheet prints only this. */
export function Receipt({ r, shop }: { r: ReceiptData; shop: string }) {
  return (
    <div className="receipt">
      <div className="r-center"><strong>{shop}</strong></div>
      <div className="r-center">{r.kind === "RETURN" ? "REFUND / 退款" : "RECEIPT / 收据"}</div>
      <div className="r-row"><span>{r.receipt_no}</span>
        <span>{new Date(r.sold_at).toLocaleString("en-MY")}</span></div>
      <div className="r-row"><span>Cashier</span><span>{r.cashier}</span></div>
      <hr />
      {r.lines.map((l, i) => (
        <div key={i}>
          <div>{l.name}</div>
          <div className="r-row"><span>  {qty(l.qty)} {l.uom} × {money(l.unit_price)}</span>
            <span>{money(l.line_total)}</span></div>
        </div>
      ))}
      <hr />
      <div className="r-row"><span>Subtotal</span><span>{money(r.subtotal)}</span></div>
      <div className="r-row"><span>SST</span><span>{money(r.tax_total)}</span></div>
      {Number(r.rounding) !== 0 && (
        <div className="r-row"><span>Rounding</span><span>{money(r.rounding)}</span></div>
      )}
      <div className="r-row r-total"><span>TOTAL</span><span>{money(r.total)}</span></div>
      {r.payments.map((p, i) => (
        <div key={i} className="r-row"><span>{p.method}</span><span>{money(p.amount)}</span></div>
      ))}
      {Number(r.change) > 0 && (
        <div className="r-row"><span>Change</span><span>{money(r.change)}</span></div>
      )}
      {r.offline && <div className="r-center">* saved offline, uploading when online *</div>}
      <div className="r-center" style={{ marginTop: 8 }}>Thank you · Terima kasih · 谢谢</div>
    </div>
  );
}
