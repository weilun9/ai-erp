import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { Picker } from "../docs/Picker";
import { errorText, today } from "../docs/shared";
import type { Key } from "../i18n";

// Promotions (applied by the till by itself) and loyalty members (points).

type Kind = "PERCENT" | "AMOUNT" | "PRICE" | "BUY_GET" | "BUNDLE";
const KINDS: Kind[] = ["PERCENT", "AMOUNT", "PRICE", "BUY_GET", "BUNDLE"];
interface Promo {
  id: string; code: string; name: string; kind: Kind; value: string; buy_qty: number; get_qty: number;
  date_from: string; date_to: string; members_only: boolean; is_active: boolean; version: number;
  item_ids: string[]; category_ids: string[]; brand_ids: string[]; location_ids: string[];
}
interface Named { id: string; code: string; name: string }
interface Member {
  id: string; card_no: string; name: string; phone: string | null; email: string | null;
  points: number; is_active: boolean; joined_at: string; version: number;
}
interface Move { kind: string; points: number; balance: number; note: string | null; created_at: string }

export function Promotions() {
  const { t, can } = useSession();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "members" ? "members" : "promos";
  return (
    <div className="docform">
      <section className="card glass">
        <h2 style={{ margin: 0 }}>{t("promosAndMembers")}</h2>
        <div className="tabs" style={{ margin: 0 }}>
          <button className={tab === "promos" ? "on" : ""} onClick={() => setParams({}, { replace: true })}>{t("promosTitle")}</button>
          <button className={tab === "members" ? "on" : ""}
                  onClick={() => setParams({ tab: "members" }, { replace: true })}>{t("membersTitle")}</button>
        </div>
        <span className="muted">{t(tab === "promos" ? "promosHint" : "membersHint")}</span>
      </section>
      {tab === "promos" ? <PromoList manage={can("pos.promo.manage")} /> : <Members manage={can("pos.member.manage")} />}
    </div>
  );
}

function status(p: Promo): Key {
  const d = today();
  if (!p.is_active) return "promoOff";
  if (d < p.date_from) return "promoUpcoming";
  if (d > p.date_to) return "promoEnded";
  return "promoRunning";
}

function describe(p: Promo, t: (k: Key) => string) {
  const v = Number(p.value);
  switch (p.kind) {
    case "PERCENT": return `${v}% ${t("promoPERCENT").replace("% ", "")}`;
    case "AMOUNT": return `${money(v)} ${t("promoAMOUNT").replace("RM ", "")}`;
    case "PRICE": return `${t("promoPRICE")} ${money(v)}`;
    case "BUY_GET": return `${t("promoBuy")} ${p.buy_qty} · ${t("promoGet")} ${p.get_qty}`;
    case "BUNDLE": return `${p.buy_qty} × ${money(v)}`;
  }
}

const blank = () => ({
  code: "", name: "", kind: "PERCENT" as Kind, value: "", buy_qty: "2", get_qty: "1",
  date_from: today(), date_to: today(), members_only: false,
  items: [] as Named[], category_ids: [] as string[], brand_ids: [] as string[], location_ids: [] as string[],
});

function PromoList({ manage }: { manage: boolean }) {
  const { t } = useSession();
  const [rows, setRows] = useState<Promo[] | null>(null);
  const [cats, setCats] = useState<Named[]>([]);
  const [brands, setBrands] = useState<Named[]>([]);
  const [stores, setStores] = useState<Named[]>([]);
  const [f, setF] = useState<ReturnType<typeof blank> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    api<Promo[]>("/promotions").then(setRows).catch((e) => { setRows([]); setError(errorText(e)); });
  }, []);
  useEffect(() => {
    load();
    api<Page<Named>>("/item-categories").then((p) => setCats(p.items)).catch(() => undefined);
    api<Page<Named>>("/item-attributes", { query: { kind: "BRAND" } }).then((p) => setBrands(p.items)).catch(() => undefined);
    api<Page<Named>>("/locations").then((p) => setStores(p.items)).catch(() => undefined);
  }, [load]);

  const unitKinds = f && (f.kind === "BUY_GET" || f.kind === "BUNDLE");
  const save = async () => {
    if (!f) return;
    setBusy(true); setError(null);
    try {
      await api("/promotions", { method: "POST", body: {
        code: f.code.trim(), name: f.name.trim(), kind: f.kind, value: f.value.trim() || "0",
        buy_qty: unitKinds ? Number(f.buy_qty) : 0, get_qty: f.kind === "BUY_GET" ? Number(f.get_qty) : 0,
        date_from: f.date_from, date_to: f.date_to, members_only: f.members_only,
        item_ids: f.items.map((x) => x.id), category_ids: f.category_ids, brand_ids: f.brand_ids,
        location_ids: f.location_ids,
      } });
      setF(null);
      load();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const toggle = async (p: Promo) => {
    try {
      await api(`/promotions/${p.id}`, { method: "PATCH", body: { version: p.version, is_active: !p.is_active } });
      load();
    } catch (e) { setError(errorText(e)); }
  };
  const pickMany = (label: string, all: Named[], chosen: string[], set: (ids: string[]) => void) => (
    <div className="field wide">
      <label>{label}</label>
      <div className="chip-pick">
        {all.map((x) => (
          <button type="button" key={x.id} className={chosen.includes(x.id) ? "on" : ""}
                  onClick={() => set(chosen.includes(x.id) ? chosen.filter((y) => y !== x.id) : [...chosen, x.id])}>
            {x.name}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <section className="card glass">
      {manage && !f && <button className="btn" style={{ alignSelf: "flex-start" }} onClick={() => setF(blank())}>+ {t("promoAdd")}</button>}
      {f && (
        <form className="promo-form" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <div className="doc-head">
            <div className="field"><label htmlFor="pr-code">{t("code")}</label>
              <input id="pr-code" className="input" value={f.code} maxLength={30} onChange={(e) => setF({ ...f, code: e.target.value })} /></div>
            <div className="field wide"><label htmlFor="pr-name">{t("name")}</label>
              <input id="pr-name" className="input" value={f.name} maxLength={100} placeholder="Buy 2 Milo get 1 free"
                     onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
            <div className="field"><label htmlFor="pr-kind">{t("promoKind")}</label>
              <select id="pr-kind" className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as Kind })}>
                {KINDS.map((k) => <option key={k} value={k}>{t(`promo${k}` as Key)}</option>)}
              </select></div>
            {f.kind !== "BUY_GET" && (
              <div className="field"><label htmlFor="pr-val">{f.kind === "PERCENT" ? "%" : "RM"}</label>
                <input id="pr-val" className="input num" inputMode="decimal" value={f.value}
                       onChange={(e) => setF({ ...f, value: e.target.value })} /></div>
            )}
            {unitKinds && (
              <div className="field"><label htmlFor="pr-buy">{f.kind === "BUNDLE" ? t("qtyLabel") : t("promoBuy")}</label>
                <input id="pr-buy" className="input num" inputMode="numeric" value={f.buy_qty}
                       onChange={(e) => setF({ ...f, buy_qty: e.target.value.replace(/\D/g, "") })} /></div>
            )}
            {f.kind === "BUY_GET" && (
              <div className="field"><label htmlFor="pr-get">{t("promoGet")}</label>
                <input id="pr-get" className="input num" inputMode="numeric" value={f.get_qty}
                       onChange={(e) => setF({ ...f, get_qty: e.target.value.replace(/\D/g, "") })} /></div>
            )}
            <div className="field"><label htmlFor="pr-from">{t("promoFrom")}</label>
              <input id="pr-from" type="date" className="input" value={f.date_from} onChange={(e) => setF({ ...f, date_from: e.target.value })} /></div>
            <div className="field"><label htmlFor="pr-to">{t("promoTo")}</label>
              <input id="pr-to" type="date" className="input" value={f.date_to} onChange={(e) => setF({ ...f, date_to: e.target.value })} /></div>
            <label className="check-row">
              <input type="checkbox" checked={f.members_only} onChange={(e) => setF({ ...f, members_only: e.target.checked })} />
              {t("promoMembers")}
            </label>
          </div>
          <div className="field wide">
            <label>{t("promoItems")}</label>
            <Picker<Named & { base_uom?: string }> placeholder={t("addItem")}
              search={(q) => api<Page<Named>>("/items", { query: { q, page_size: 8 } }).then((p) => p.items)}
              label={(i) => `${i.code} · ${i.name}`}
              onPick={(i) => setF({ ...f, items: f.items.some((x) => x.id === i.id) ? f.items : [...f.items, i] })} />
            <div className="chip-pick">
              {f.items.map((i) => (
                <button type="button" key={i.id} className="on" onClick={() => setF({ ...f, items: f.items.filter((x) => x.id !== i.id) })}>
                  {i.name} ✕
                </button>
              ))}
            </div>
          </div>
          {cats.length > 0 && pickMany(t("promoCats"), cats, f.category_ids, (ids) => setF({ ...f, category_ids: ids }))}
          {brands.length > 0 && pickMany(t("promoBrands"), brands, f.brand_ids, (ids) => setF({ ...f, brand_ids: ids }))}
          {stores.length > 1 && pickMany(t("promoStores"), stores, f.location_ids, (ids) => setF({ ...f, location_ids: ids }))}
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="btn ghost" onClick={() => setF(null)}>{t("cancel")}</button>
            <button className="btn" disabled={busy || !f.code.trim() || !f.name.trim()}>{t("save")}</button>
          </div>
        </form>
      )}
      {error && <div className="error" role="alert">{error}</div>}
      {!rows ? <span className="muted">{t("loading")}</span> : rows.length === 0 ? null : (
        <div className="scroll-x">
          <table className="table one-line">
            <thead><tr><th>{t("code")}</th><th>{t("name")}</th><th>{t("promoKind")}</th>
              <th>{t("promoFrom")}</th><th>{t("promoTo")}</th><th /><th /></tr></thead>
            <tbody>
              {rows.map((p) => {
                const st = status(p);
                return (
                  <tr key={p.id} className={st === "promoRunning" ? "" : "muted"}>
                    <td>{p.code}</td>
                    <td>{p.name}{p.members_only && <span className="badge audit-badge">★ {t("promoMembers")}</span>}</td>
                    <td>{describe(p, t)}</td>
                    <td>{p.date_from}</td><td>{p.date_to}</td>
                    <td><span className={`badge${st === "promoRunning" ? " green" : ""}`}>{t(st)}</span></td>
                    <td className="num">
                      {manage && <button className="chip-btn" onClick={() => toggle(p)}>{t(p.is_active ? "deactivate" : "activate")}</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Members({ manage }: { manage: boolean }) {
  const { t } = useSession();
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<Member[] | null>(null);
  const [f, setF] = useState<{ card_no: string; name: string; phone: string } | null>(null);
  const [open, setOpen] = useState<Member | null>(null);
  const [moves, setMoves] = useState<Move[]>([]);
  const [adj, setAdj] = useState({ points: "", note: "" });
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Member[]>("/members", { query: { q: q.trim() || undefined } }).then(setRows)
      .catch((e) => { setRows([]); setError(errorText(e)); });
  }, [q]);
  useEffect(() => { const h = setTimeout(load, q ? 250 : 0); return () => clearTimeout(h); }, [load, q]);
  const show = (m: Member) => {
    setOpen(m);
    api<Move[]>(`/members/${m.id}/points`).then(setMoves).catch(() => setMoves([]));
  };
  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); load(); } catch (e) { setError(errorText(e)); }
  };
  return (
    <div className="two">
      <section className="card glass">
        <div className="row">
          <input className="input" style={{ flex: 1 }} value={q} placeholder={t("search")} onChange={(e) => setQ(e.target.value)} />
          {manage && <button className="btn" onClick={() => setF({ card_no: "", name: "", phone: "" })}>+ {t("memberAdd")}</button>}
        </div>
        {f && (
          <form className="doc-head" onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              await api("/members", { method: "POST", body: { card_no: f.card_no.trim(), name: f.name.trim(),
                                                                 phone: f.phone.trim() || null } });
              setF(null);
            });
          }}>
            <div className="field"><label htmlFor="m-card">{t("memberCard")} #</label>
              <input id="m-card" className="input" autoFocus value={f.card_no} onChange={(e) => setF({ ...f, card_no: e.target.value })} /></div>
            <div className="field"><label htmlFor="m-name">{t("name")}</label>
              <input id="m-name" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
            <div className="field"><label htmlFor="m-phone">{t("phone")}</label>
              <input id="m-phone" className="input" inputMode="tel" value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} /></div>
            <button className="btn" style={{ alignSelf: "end" }} disabled={!f.card_no.trim() || !f.name.trim()}>{t("save")}</button>
          </form>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {!rows ? <span className="muted">{t("loading")}</span> : (
          <table className="table one-line">
            <thead><tr><th>{t("memberCard")} #</th><th>{t("name")}</th><th>{t("phone")}</th><th className="num">{t("memberPoints")}</th></tr></thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id} className={`click${open?.id === m.id ? " picked" : ""}`} onClick={() => show(m)}>
                  <td>{m.card_no}</td><td>{m.name}</td><td>{m.phone ?? "—"}</td><td className="num">{m.points}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card glass">
        {!open ? <span className="muted">{t("membersHint")}</span> : (
          <>
            <h3 style={{ margin: 0 }}>★ {open.name}</h3>
            <span className="muted">{open.card_no} · {rows?.find((x) => x.id === open.id)?.points ?? open.points} {t("points")}</span>
            {manage && (
              <form className="row" style={{ flexWrap: "wrap" }} onSubmit={(e) => {
                e.preventDefault();
                run(async () => {
                  await api(`/members/${open.id}/adjust`, { method: "POST", body: { points: Number(adj.points), note: adj.note.trim() } });
                  setAdj({ points: "", note: "" });
                  show(open);
                });
              }}>
                <input className="input sm num" style={{ width: 100 }} inputMode="numeric" placeholder="±100" value={adj.points}
                       aria-label={t("memberAdjust")} onChange={(e) => setAdj({ ...adj, points: e.target.value.replace(/[^\d-]/g, "") })} />
                <input className="input sm" style={{ flex: 1 }} placeholder={t("remark")} value={adj.note}
                       onChange={(e) => setAdj({ ...adj, note: e.target.value })} />
                <button className="chip-btn" disabled={!Number(adj.points) || !adj.note.trim()}>{t("memberAdjust")}</button>
              </form>
            )}
            <h3>{t("memberHistory")}</h3>
            <table className="table one-line">
              <tbody>
                {moves.map((m, i) => (
                  <tr key={i}>
                    <td>{new Date(m.created_at).toLocaleDateString("en-CA")}</td>
                    <td>{m.kind}{m.note && <span className="muted"> · {m.note}</span>}</td>
                    <td className={`num ${m.points < 0 ? "neg" : ""}`}>{m.points > 0 ? "+" : ""}{m.points}</td>
                    <td className="num muted">{m.balance}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  );
}
