import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText } from "../docs/shared";
import type { Key } from "../i18n";

// Customers and suppliers: a searchable list and one form for both. The two share almost
// every field (name, address, tax identity for e-invoices, payment terms); customers add a
// credit limit, suppliers add notes on how their paperwork looks.

type PartyType = "customer" | "supplier";
interface Party {
  id: string; code: string; name: string; is_active: boolean; version: number; term_days: number;
  city?: string | null; phone?: string | null; credit_limit?: string | null;
  profile?: Record<string, unknown>;
  [k: string]: unknown;
}

const isType = (x: string | undefined): x is PartyType => x === "customer" || x === "supplier";
const path = (type: PartyType) => `/${type}s`;
const back = (type: PartyType) => (type === "customer" ? "/sales" : "/purchases");

// LHDN e-invoice state codes (Malaysia)
const STATES: [string, string][] = [
  ["01", "Johor"], ["02", "Kedah"], ["03", "Kelantan"], ["04", "Melaka"], ["05", "Negeri Sembilan"],
  ["06", "Pahang"], ["07", "Pulau Pinang"], ["08", "Perak"], ["09", "Perlis"], ["10", "Selangor"],
  ["11", "Terengganu"], ["12", "Sabah"], ["13", "Sarawak"], ["14", "W.P. Kuala Lumpur"],
  ["15", "W.P. Labuan"], ["16", "W.P. Putrajaya"], ["17", "Not applicable"],
];
const ID_TYPES = ["BRN", "NRIC", "PASSPORT", "ARMY"];

// --- list ---------------------------------------------------------------------------------

export function PartyList() {
  const { type } = useParams();
  const { t, can } = useSession();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const all = params.get("all") === "1";
  const [text, setText] = useState(q);
  const [rows, setRows] = useState<Party[] | null>(null);
  const ok = isType(type);

  useEffect(() => {
    if (!ok) return;
    setRows(null);
    // the API lists active ones unless told otherwise; "show inactive" asks for both
    const get = (active: boolean) =>
      api<Page<Party>>(path(type), { query: { q, page_size: 100, active } }).then((p) => p.items);
    Promise.all([get(true), all ? get(false) : Promise.resolve([])])
      .then(([on, off]) => setRows([...on, ...off]))
      .catch(() => setRows([]));
  }, [ok, type, q, all]);

  if (!ok) return <section className="card glass">{"Unknown list"}</section>;
  if (!can(`md.${type}.read`)) {
    return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  }
  return (
    <section className="card glass">
      <div className="list-head">
        <div>
          <Link to={back(type)} className="muted" style={{ textDecoration: "none" }}>
            ← {t(type === "customer" ? "sales" : "purchases")}
          </Link>
          <h2 style={{ margin: "4px 0 0" }}>{t(`${type}s` as Key)}</h2>
        </div>
        {can(`md.${type}.manage`) && (
          <Link className="btn" style={{ textDecoration: "none", whiteSpace: "nowrap" }}
                to={`/parties/${type}/new`}>+ {t("newDoc")}</Link>
        )}
      </div>
      <form className="row" style={{ flexWrap: "wrap" }}
            onSubmit={(e) => { e.preventDefault(); setParams({ ...(text ? { q: text } : {}), ...(all ? { all: "1" } : {}) }); }}>
        <input className="input" style={{ flex: 1, minWidth: 200 }} value={text}
               placeholder={t("searchParty")} aria-label={t("searchParty")}
               onChange={(e) => setText(e.target.value)} />
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={all}
                 onChange={(e) => setParams({ ...(q ? { q } : {}), ...(e.target.checked ? { all: "1" } : {}) })} />
          <span>{t("showInactive")}</span>
        </label>
      </form>
      {!rows ? <span className="muted">{t("loading")}</span>
        : rows.length === 0 ? <span className="muted">{t("noResults")}</span> : (
          <div className="scroll-x">
            <table className="table one-line">
              <thead>
                <tr>
                  <th>{t("code")}</th><th>{t("name")}</th><th>{t("city")}</th><th>{t("phone")}</th>
                  <th className="num">{t("termDays")}</th>
                  {type === "customer" && <th className="num">{t("creditLimit")}</th>}
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="click" onClick={() => nav(`/parties/${type}/${r.id}`)}>
                    <td>{r.code}</td>
                    <td>{r.name}</td>
                    <td>{r.city ?? ""}</td>
                    <td>{r.phone ?? ""}</td>
                    <td className="num">{r.term_days}</td>
                    {type === "customer" && <td className="num">{r.credit_limit != null ? money(r.credit_limit) : "—"}</td>}
                    <td>{!r.is_active && <span className="badge">{t("inactive")}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}

// --- form ---------------------------------------------------------------------------------

type FieldDef = { key: string; label: Key; wide?: boolean; max?: number; kind?: "state" | "idtype" | "int" | "money" };
const IDENTITY: FieldDef[] = [
  { key: "code", label: "code", max: 30 },
  { key: "name", label: "name", wide: true, max: 200 },
  { key: "contact_person", label: "contactPerson", max: 100 },
  { key: "phone", label: "phone", max: 30 },
  { key: "email", label: "email", wide: true, max: 200 },
];
const ADDRESS: FieldDef[] = [
  { key: "address1", label: "address1", wide: true, max: 150 },
  { key: "address2", label: "address2", wide: true, max: 150 },
  { key: "address3", label: "address3", wide: true, max: 150 },
  { key: "postcode", label: "postcode", max: 10 },
  { key: "city", label: "city", max: 50 },
  { key: "state_code", label: "state", kind: "state" },
];
const TAX: FieldDef[] = [
  { key: "tin", label: "tin", max: 20 },
  { key: "id_type", label: "idType", kind: "idtype" },
  { key: "id_no", label: "idNo", max: 30 },
  { key: "sst_no", label: "sstNo", max: 50 },
  { key: "msic_code", label: "msic", max: 5 },
];
const TERMS_CUSTOMER: FieldDef[] = [
  { key: "term_days", label: "termDays", kind: "int" },
  { key: "credit_limit", label: "creditLimit", kind: "money" },
  { key: "price_level", label: "priceLevel", kind: "int" },
];
const TERMS_SUPPLIER: FieldDef[] = [{ key: "term_days", label: "termDays", kind: "int" }];
// supplier habits the intake checks and the AI butler read
const PROFILE: FieldDef[] = [
  { key: "invoice_no_pattern", label: "invoicePattern", wide: true },
  { key: "uom_notes", label: "uomNotes", wide: true },
  { key: "settlement_notes", label: "settlementNotes", wide: true },
  { key: "tax_notes", label: "taxNotes", wide: true },
];

const str = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "string" && /^-?\d+\.\d{3,}$/.test(v)
  ? String(Number(v)) : String(v));

export function PartyForm() {
  const { type, id } = useParams();
  const { t, can } = useSession();
  const nav = useNavigate();
  const ok = isType(type);
  const isNew = id === "new";
  const [loaded, setLoaded] = useState<Party | null>(null);
  const [v, setV] = useState<Record<string, string>>({ term_days: "30" });
  const [prof, setProf] = useState<Record<string, string>>({});
  const [active, setActive] = useState(true);
  const [loading, setLoading] = useState(!isNew);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const take = (p: Party) => {
    setLoaded(p);
    const all = [...IDENTITY, ...ADDRESS, ...TAX, ...TERMS_CUSTOMER];
    setV(Object.fromEntries(all.filter((f) => p[f.key] !== undefined).map((f) => [f.key, str(p[f.key])])));
    setProf(Object.fromEntries(PROFILE.map((f) => [f.key, str(p.profile?.[f.key])])));
    setActive(p.is_active);
  };
  useEffect(() => {
    if (!ok || isNew) return;
    setLoading(true);
    api<Party>(`${path(type)}/${id}`)
      .then(take)
      .catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ok, type, id, isNew]);

  if (!ok) return <section className="card glass">{"Unknown list"}</section>;
  const editable = can(`md.${type}.manage`);
  const terms = type === "customer" ? TERMS_CUSTOMER : TERMS_SUPPLIER;
  const fields = [...IDENTITY, ...ADDRESS, ...TAX, ...terms];
  // a field the server hid from this user (personal data, finance) is not on the form at all
  const visible = (f: FieldDef) => isNew || !loaded || loaded[f.key] !== undefined;

  const value = (f: FieldDef, raw: string): unknown => {
    const s = raw.trim();
    if (s === "") return null;
    return f.kind === "int" ? Number(s) : s;
  };
  const save = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (!(v.code ?? "").trim() || !(v.name ?? "").trim()) throw new Error(t("codeNameRequired"));
      if (v.term_days && !/^\d+$/.test(v.term_days.trim())) throw new Error(t("termDays"));
      const body: Record<string, unknown> = {};
      for (const f of fields.filter(visible)) {
        const now = value(f, v[f.key] ?? "");
        const was = loaded ? value(f, str(loaded[f.key])) : null;
        if (isNew ? now !== null : now !== was) body[f.key] = now;
      }
      if (type === "supplier") {
        const next = { ...(loaded?.profile ?? {}) };
        let changed = false;
        for (const f of PROFILE) {
          const now = (prof[f.key] ?? "").trim() || null;
          if (now !== ((loaded?.profile?.[f.key] as string | null | undefined) ?? null)) changed = true;
          next[f.key] = now;
        }
        if (changed) body.profile = next;
      }
      if (isNew) {
        const made = await api<Party>(path(type), { method: "POST", body });
        nav(`/parties/${type}/${made.id}`, { replace: true });
        take(made);
      } else if (loaded) {
        if (active !== loaded.is_active) body.is_active = active;
        if (Object.keys(body).length === 0) { setNotice(t("nothingChanged")); return; }
        take(await api<Party>(`${path(type)}/${loaded.id}`,
          { method: "PATCH", body: { version: loaded.version, ...body } }));
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
        <Link to={`/parties/${type}`} className="muted" style={{ textDecoration: "none" }}>← {t("back")}</Link>
        <div className="error" role="alert">{loadError ?? t("noAccess")}</div>
      </section>
    );
  }

  const input = (f: FieldDef, get: Record<string, string>, set: (k: string, x: string) => void) => (
    <div key={f.key} className={`field${f.wide ? " wide" : ""}`}>
      <label htmlFor={`f-${f.key}`}>{t(f.label)}</label>
      {f.kind === "state" || f.kind === "idtype" ? (
        <select id={`f-${f.key}`} className="input" value={get[f.key] ?? ""} disabled={!editable}
                onChange={(e) => set(f.key, e.target.value)}>
          <option value="">—</option>
          {f.kind === "state"
            ? STATES.map(([c, n]) => <option key={c} value={c}>{c} · {n}</option>)
            : ID_TYPES.map((x) => <option key={x}>{x}</option>)}
        </select>
      ) : (
        <input id={`f-${f.key}`} className="input" value={get[f.key] ?? ""} disabled={!editable}
               maxLength={f.max} inputMode={f.kind === "int" ? "numeric" : f.kind === "money" ? "decimal" : undefined}
               onChange={(e) => set(f.key, e.target.value)} />
      )}
    </div>
  );
  const setField = (k: string, x: string) => setV((m) => ({ ...m, [k]: x }));
  const setProfile = (k: string, x: string) => setProf((m) => ({ ...m, [k]: x }));
  const group = (title: Key, defs: FieldDef[]) => {
    const shown = defs.filter(visible);
    return shown.length > 0 && (
      <>
        <h3>{t(title)}</h3>
        <div className="doc-head">{shown.map((f) => input(f, v, setField))}</div>
      </>
    );
  };

  return (
    <div className="docform">
      <section className="card glass">
        <div className="row">
          <div>
            <Link to={`/parties/${type}`} className="muted" style={{ textDecoration: "none" }}>
              ← {t(`${type}s` as Key)}
            </Link>
            <h2 style={{ margin: "4px 0 0" }}>{isNew ? `${t("newDoc")} ${t(type).toLowerCase()}` : loaded?.name}</h2>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {!editable && <span className="muted nowrap">{t("readOnly")}</span>}
            {loaded && can("fin.doc.read") && (
              <Link className="chip-btn" to={`/accounts?type=${type}&party=${loaded.id}`}>{t("accounts")}</Link>
            )}
          </div>
        </div>
        <div className="doc-head">{IDENTITY.filter(visible).map((f) => input(f, v, setField))}</div>
        {group("addressGroup", ADDRESS)}
        {group("taxGroup", TAX)}
        {group("termsGroup", terms)}
        {type === "supplier" && (
          <>
            <h3>{t("profileGroup")}</h3>
            <div className="doc-head">{PROFILE.map((f) => input(f, prof, setProfile))}</div>
          </>
        )}
        {!isNew && editable && (
          <label className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            <span>{t("activeHint")}</span>
          </label>
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
