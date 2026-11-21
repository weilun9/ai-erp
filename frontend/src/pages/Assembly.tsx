import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, money, qty as fmtQty } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { errorText, today } from "../docs/shared";

// Bills of materials and assembly: the editor sits on the item form; the page makes or
// breaks up a product at a store.

interface Named { id: string; code: string; name: string; base_uom?: string }
interface BomLine { item_id: string; qty: string }
interface Bom { id: string; item_id: string; output_qty: string; note: string | null; lines: BomLine[] }
interface Need { item_id: string; qty: string; on_hand: string }
interface Asm {
  id: string; doc_no: string; doc_date: string; location_id: string; item_id: string;
  direction: "ASSEMBLE" | "DISASSEMBLE"; qty: string; unit_cost: string; cost_total: string;
  status: string; version: number;
}

function useNames() {
  const [names, setNames] = useState<Record<string, Named>>({});
  const want = useCallback((ids: string[]) => {
    const missing = ids.filter((x) => !names[x]);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api<Named>(`/items/${id}`).catch(() => null))).then((xs) =>
      setNames((n) => ({ ...n, ...Object.fromEntries(xs.filter(Boolean).map((x) => [x!.id, x!])) })));
  }, [names]);
  return { names, want };
}

/** The item form's "Bill of materials" section. */
export function BomEditor({ itemId, baseUom, editable }: { itemId: string; baseUom: string; editable: boolean }) {
  const { t } = useSession();
  const [lines, setLines] = useState<(BomLine & { name: string; uom: string })[] | null>(null);
  const [output, setOutput] = useState("1");
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { names, want } = useNames();

  useEffect(() => {
    api<Bom | null>(`/items/${itemId}/bom`).then((b) => {
      setOutput(b ? String(Number(b.output_qty)) : "1");
      setLines((b?.lines ?? []).map((l) => ({ ...l, qty: String(Number(l.qty)), name: "", uom: "" })));
      if (b) want(b.lines.map((l) => l.item_id));
    }).catch((e) => setError(errorText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemId]);

  const save = async () => {
    setError(null); setMsg(null);
    try {
      await api(`/items/${itemId}/bom`, { method: "PUT", body: {
        output_qty: output || "1", lines: (lines ?? []).map((l) => ({ item_id: l.item_id, qty: l.qty })) } });
      setDirty(false); setMsg(t("bomSaved"));
    } catch (e) { setError(errorText(e)); }
  };
  if (!lines) return null;
  return (
    <section className="card glass">
      <div className="row">
        <h3 style={{ margin: 0 }}>{t("bomTitle")}</h3>
        {lines.length > 0 && <Link className="chip-btn" to={`/assembly?item=${itemId}`}>{t("asmOpen")}</Link>}
      </div>
      <span className="muted">{t("bomHint")}</span>
      <div className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
        <span>{t("bomMakes")}</span>
        <input className="input sm num" style={{ width: 90 }} inputMode="decimal" value={output} disabled={!editable}
               aria-label={t("bomMakes")} onChange={(e) => { setOutput(e.target.value); setDirty(true); }} />
        <span className="muted">{baseUom} {t("bomTakes")}</span>
      </div>
      {lines.length > 0 && (
        <table className="table one-line">
          <tbody>
            {lines.map((l, i) => {
              const it = names[l.item_id];
              return (
                <tr key={l.item_id}>
                  <td>{it ? `${it.code} · ${it.name}` : l.name || "…"}</td>
                  <td className="num" style={{ width: 130 }}>
                    <input className="input sm num" inputMode="decimal" value={l.qty} disabled={!editable}
                           aria-label={t("qtyLabel")}
                           onChange={(e) => { setLines(lines.map((x, j) => (j === i ? { ...x, qty: e.target.value } : x))); setDirty(true); }} />
                  </td>
                  <td className="muted">{it?.base_uom ?? l.uom}</td>
                  <td className="num">{editable && (
                    <button className="chip-btn" onClick={() => { setLines(lines.filter((_, j) => j !== i)); setDirty(true); }}>✕</button>
                  )}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {editable && (
        <>
          <Picker<Named> placeholder={t("bomAdd")}
            search={(q) => api<Page<Named>>("/items", { query: { q, page_size: 8 } }).then((p) => p.items.filter((x) => x.id !== itemId))}
            label={(i) => `${i.code} · ${i.name}`}
            onPick={(i) => {
              if (lines.some((x) => x.item_id === i.id)) return;
              setLines([...lines, { item_id: i.id, qty: "1", name: `${i.code} · ${i.name}`, uom: i.base_uom ?? "" }]);
              setDirty(true);
            }} />
          <div className="row" style={{ justifyContent: "flex-end" }}>
            {msg && !dirty && <span className="ok-text">{msg}</span>}
            <button className="btn" disabled={!dirty} onClick={save}>{t("save")}</button>
          </div>
        </>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

export function AssemblyPage() {
  const { t, can } = useSession();
  const [boms, setBoms] = useState<Bom[]>([]);
  const [stores, setStores] = useState<Named[]>([]);
  const [item, setItem] = useState(new URLSearchParams(location.search).get("item") ?? "");
  const [qty, setQty] = useState("1");
  const [store, setStore] = useState("");
  const [direction, setDirection] = useState<"ASSEMBLE" | "DISASSEMBLE">("ASSEMBLE");
  const [needs, setNeeds] = useState<Need[] | null>(null);
  const [rows, setRows] = useState<Asm[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);  // bumps after each assembly: stock figures change
  const { names, want } = useNames();
  const write = can("inv.doc.create") && can("inv.doc.post");

  const load = useCallback(() => {
    api<Asm[]>("/inventory/assemblies").then((xs) => { setRows(xs); want(xs.map((x) => x.item_id)); }).catch(() => undefined);
  }, [want]);
  useEffect(() => {
    api<Bom[]>("/boms").then((b) => { setBoms(b); want(b.map((x) => x.item_id)); }).catch((e) => setError(errorText(e)));
    api<Page<Named>>("/locations").then((p) => { setStores(p.items); setStore((s) => s || p.items[0]?.id || ""); }).catch(() => undefined);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    setNeeds(null);
    if (!item || !store || !(Number(qty) > 0)) return;
    api<Need[]>(`/items/${item}/bom/needs`, { query: { qty, location_id: store } })
      .then((n) => { setNeeds(n); want(n.map((x) => x.item_id)); }).catch((e) => setError(errorText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item, store, qty, tick]);

  const short = direction === "ASSEMBLE" && (needs ?? []).some((n) => Number(n.on_hand) < Number(n.qty));
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); load(); setTick((n) => n + 1); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const label = (id: string) => (names[id] ? `${names[id].code} · ${names[id].name}` : "…");

  return (
    <div className="two">
      <section className="card glass">
        <h2 style={{ margin: 0 }}>{t("asmTitle")}</h2>
        <span className="muted">{t("asmHint")}</span>
        {boms.length === 0 ? <span className="muted">{t("asmNoBoms")}</span> : (
          <>
            <div className="tabs" style={{ margin: 0 }}>
              <button className={direction === "ASSEMBLE" ? "on" : ""} onClick={() => setDirection("ASSEMBLE")}>{t("asmAssemble")}</button>
              <button className={direction === "DISASSEMBLE" ? "on" : ""} onClick={() => setDirection("DISASSEMBLE")}>{t("asmDisassemble")}</button>
            </div>
            <div className="doc-head">
              <div className="field wide"><label htmlFor="asm-item">{t("item")}</label>
                <select id="asm-item" className="input" value={item} onChange={(e) => setItem(e.target.value)}>
                  <option value="">—</option>
                  {boms.map((b) => <option key={b.item_id} value={b.item_id}>{label(b.item_id)}</option>)}
                </select></div>
              <div className="field"><label htmlFor="asm-qty">{t("qtyLabel")}</label>
                <input id="asm-qty" className="input num" inputMode="decimal" value={qty} onChange={(e) => setQty(e.target.value)} /></div>
              <div className="field"><label htmlFor="asm-store">{t("store")}</label>
                <select id="asm-store" className="input" value={store} onChange={(e) => setStore(e.target.value)}>
                  {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
                </select></div>
            </div>
            {needs && (
              <table className="table one-line">
                <thead><tr><th>{direction === "ASSEMBLE" ? t("asmUses") : t("asmGives")}</th>
                  <th className="num">{t("qtyLabel")}</th><th className="num">{t("asmOnHand")}</th></tr></thead>
                <tbody>
                  {needs.map((n) => (
                    <tr key={n.item_id}>
                      <td>{label(n.item_id)}</td>
                      <td className="num">{fmtQty(n.qty)}</td>
                      <td className={`num ${direction === "ASSEMBLE" && Number(n.on_hand) < Number(n.qty) ? "neg" : ""}`}>{fmtQty(n.on_hand)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {short && <div className="warn-box">{t("asmShort")}</div>}
            {write && (
              <button className="btn" style={{ alignSelf: "flex-start" }} disabled={busy || !item || !store || !(Number(qty) > 0) || short}
                      onClick={() => run(() => api("/inventory/assemblies", { method: "POST", body: {
                        item_id: item, qty, location_id: store, doc_date: today(), direction } }))}>
                {direction === "ASSEMBLE" ? t("asmAssemble") : t("asmDisassemble")}
              </button>
            )}
          </>
        )}
        {error && <div className="error" role="alert">{error}</div>}
      </section>
      <section className="card glass">
        <h3 style={{ margin: 0 }}>{t("asmRecent")}</h3>
        <table className="table one-line">
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={r.status === "VOIDED" ? "muted" : ""}>
                <td>{r.doc_no}<div className="muted small">{r.doc_date}</div></td>
                <td>{r.direction === "ASSEMBLE" ? "＋" : "－"} {fmtQty(r.qty)} × {label(r.item_id)}</td>
                <td className="num">{can("field.cost.read") ? money(r.cost_total) : ""}</td>
                <td className="num">
                  {r.status === "POSTED" && can("inv.doc.void") ? (
                    <button className="chip-btn" disabled={busy}
                            onClick={() => run(() => api(`/inventory/assemblies/${r.id}/void`, {
                              method: "POST", body: { version: r.version, reason: "voided" } }))}>{t("voidDoc")}</button>
                  ) : r.status === "VOIDED" ? t("lotVoided") : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
