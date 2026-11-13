// Helpers shared by the document entry screens.
import { ApiError } from "../api";
import type { T } from "../i18n";

// where each document type code opens (fees have no screen of their own)
export const DOC_ROUTE: Record<string, string> = {
  INV: "sales-invoice", CN: "customer-cn", OR: "receipt",
  GRN: "grn", SCN: "supplier-cn", PV: "supplier-payment",
};

/** A plain 2-decimal number for tables on paper (the column heading carries the currency). */
export const amt = (v: string | number) =>
  Number(v).toLocaleString("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const today = () => new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD, local
export const round2 = (x: number) => Math.round(x * 100) / 100;
export const isNum = (s: string) => s.trim() !== "" && Number.isFinite(Number(s));

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const extra = Object.entries(e.details)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join(", ")
        : typeof v === "object" ? JSON.stringify(v) : v}`)
      .join(" · ");
    return extra ? `${e.message} (${extra})` : e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

export interface Warning { code: string; message: string; line?: number; [k: string]: unknown }

// warnings the server sends on save, in the user's language (unknown codes: server text)
const WARN_KEYS = {
  CREDIT_LIMIT: "warnCREDIT_LIMIT",
  BELOW_COST: "warnBELOW_COST",
  BELOW_MIN_PRICE: "warnBELOW_MIN_PRICE",
  SUPPLIER_REF_FORMAT: "warnSUPPLIER_REF_FORMAT",
  SUPPLIER_REF_MISSING: "warnSUPPLIER_REF_MISSING",
  ADVANCE: "warnADVANCE",
  PAYMENT_NOT_FOUND: "warnPAYMENT_NOT_FOUND",
  PAYMENT_NOT_READY: "warnPAYMENT_NOT_READY",
  PAYMENT_AMOUNT_DIFF: "warnPAYMENT_AMOUNT_DIFF",
  SUPPLIER_REFUND: "warnSUPPLIER_REFUND",
} as const;

export function warnText(t: T, w: Warning): string {
  const key = WARN_KEYS[w.code as keyof typeof WARN_KEYS];
  const text = key ? t(key) : w.message;
  return w.line ? `${t("lineNo").replace("{n}", String(w.line))}: ${text}` : text;
}
