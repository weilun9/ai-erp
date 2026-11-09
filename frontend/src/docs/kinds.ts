// The six document kinds the entry form handles. Sales and purchase documents share one
// lifecycle (DRAFT -> SAVED -> POSTED -> VOIDED) and one line shape; what differs is listed here.

export type Area = "sales" | "purchase";

export const SALES_KINDS = ["sales-invoice", "delivery-order", "quotation", "customer-cn"] as const;
export const PURCHASE_KINDS = ["grn", "supplier-cn"] as const;
export type Kind = (typeof SALES_KINDS)[number] | (typeof PURCHASE_KINDS)[number];

export interface KindDef {
  kind: Kind;
  area: Area;
  api: string;                      // collection path
  party: "customer" | "supplier";
  perm: "sal" | "pur";              // permission prefix: <perm>.doc.create / post / void
  posts: boolean;                   // quotations are converted, never posted
  priceRequired: boolean;           // a GRN line must carry the supplier's price
  priceField: "selling_price" | "purchase_price";
  taxField: "sales_tax_code_id" | "purchase_tax_code_id";
  refField: "customer_ref_no" | "supplier_ref_no";
  // documents this one can become, and the status it must have first
  converts: { target: Kind; from: "SAVED" | "POSTED" }[];
}

const sales = (kind: Kind, extra: Partial<KindDef> = {}): KindDef => ({
  kind, area: "sales", api: `/sales/${kind}`, party: "customer", perm: "sal", posts: true,
  priceRequired: false, priceField: "selling_price", taxField: "sales_tax_code_id",
  refField: "customer_ref_no", converts: [], ...extra,
});
const purchase = (kind: Kind, extra: Partial<KindDef> = {}): KindDef => ({
  kind, area: "purchase", api: `/purchase/${kind}`, party: "supplier", perm: "pur", posts: true,
  priceRequired: false, priceField: "purchase_price", taxField: "purchase_tax_code_id",
  refField: "supplier_ref_no", converts: [], ...extra,
});

export const KINDS: Record<Kind, KindDef> = {
  quotation: sales("quotation", {
    posts: false,
    converts: [{ target: "delivery-order", from: "SAVED" }, { target: "sales-invoice", from: "SAVED" }],
  }),
  "delivery-order": sales("delivery-order", {
    converts: [{ target: "sales-invoice", from: "POSTED" }],
  }),
  "sales-invoice": sales("sales-invoice", {
    converts: [{ target: "customer-cn", from: "POSTED" }],
  }),
  "customer-cn": sales("customer-cn"),
  grn: purchase("grn", { priceRequired: true }),
  "supplier-cn": purchase("supplier-cn"),
};

export const isKind = (k: string | undefined): k is Kind => !!k && k in KINDS;

export const STATUS_COLOR: Record<string, string> = {
  POSTED: "green", SAVED: "amber", DRAFT: "", VOIDED: "red",
};
