// The till's offline copy of the item list (from GET /pos/catalog): enough to scan,
// price and tax a basket with no connection. Refreshed whenever the till opens online.

import { api } from "../api";
import { keysFor } from "./barcode";

export interface CatalogItem {
  id: string;
  code: string;
  name: string;
  base_uom: string;
  tax_rate: string;
  units: { uom: string; factor: string; price: string | null }[];
  barcodes: { barcode: string; uom: string; keys: string[] }[];
}

const KEY = "pos-catalog";
let items: CatalogItem[] | null = null;
let byKey: Map<string, { item: CatalogItem; uom: string }> | null = null;

function load(): CatalogItem[] {
  if (items) return items;
  try {
    items = (JSON.parse(localStorage.getItem(KEY) ?? "null")?.items ?? []) as CatalogItem[];
  } catch {
    items = [];
  }
  return items;
}

function index() {
  if (byKey) return byKey;
  byKey = new Map();
  for (const item of load()) {
    for (const b of item.barcodes) for (const k of b.keys) byKey.set(k, { item, uom: b.uom });
  }
  return byKey;
}

export async function refreshCatalog(): Promise<number> {
  const data = await api<{ generated_at: string; items: CatalogItem[] }>("/pos/catalog");
  try {
    localStorage.setItem(KEY, JSON.stringify(data));
  } catch {
    /* too big for this browser's storage: offline scanning falls back to recent items */
  }
  items = data.items;
  byKey = null;
  return data.items.length;
}

export function findBarcode(text: string): { item: CatalogItem; uom: string } | null {
  const idx = index();
  for (const k of keysFor(text)) {
    const hit = idx.get(k);
    if (hit) return hit;
  }
  return null;
}

export function searchName(text: string, limit = 8): CatalogItem[] {
  const q = text.trim().toLowerCase();
  return load().filter((i) => i.code.toLowerCase().includes(q) || i.name.toLowerCase().includes(q))
    .slice(0, limit);
}

export function unitPrice(itemId: string, uom: string): { price: string; rate: string } | null {
  const item = load().find((i) => i.id === itemId);
  const unit = item?.units.find((u) => u.uom === uom);
  return item && unit?.price ? { price: unit.price, rate: item.tax_rate } : null;
}
