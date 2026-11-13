import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { errorText } from "../docs/shared";
import type { Key } from "../i18n";

// Settings: the reference lists the documents use (tax codes, units, item categories) and
// the administration of who may log in (users, their tags, the approved computers), plus
// the company profile that goes on printed documents and e-invoices.
// On the public demo the server refuses changes to people, devices and the company.

type Tab = "tax" | "units" | "categories" | "groups" | "users" | "tags" | "devices" | "company";
const TABS: { id: Tab; label: Key; perm: string }[] = [
  { id: "tax", label: "setTax", perm: "md.setup.read" },
  { id: "units", label: "setUnits", perm: "md.setup.read" },
  { id: "categories", label: "setCategories", perm: "md.setup.read" },
  { id: "groups", label: "setItemGroups", perm: "md.setup.read" },
  { id: "users", label: "setUsers", perm: "core.user.read" },
  { id: "tags", label: "setTags", perm: "core.tag.read" },
  { id: "devices", label: "setDevices", perm: "core.device.manage" },
  { id: "company", label: "setCompany", perm: "core.company.read" },
];

/** Loads a list, and runs changes with one shared busy / error state. */
function useList<T>(path: string, unwrap: (x: unknown) => T[]) {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    api<unknown>(path).then((x) => setRows(unwrap(x))).catch((e) => { setRows([]); setError(errorText(e)); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  useEffect(load, [load]);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      load();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { rows, error, busy, run };
}
const paged = <T,>(x: unknown) => (x as Page<T>).items;

export function Settings() {
  const { t, can } = useSession();
  const [params, setParams] = useSearchParams();
  const allowed = TABS.filter((x) => can(x.perm));
  const tab = allowed.find((x) => x.id === params.get("tab"))?.id ?? allowed[0]?.id;
  if (!tab) return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  return (
    <div className="docform">
      <section className="card glass">
        <h2 style={{ margin: 0 }}>{t("settings")}</h2>
        <div className="tabs settings-tabs" style={{ margin: 0 }}>
          {allowed.map((x) => (
            <button key={x.id} className={x.id === tab ? "on" : ""}
                    onClick={() => setParams({ tab: x.id }, { replace: true })}>{t(x.label)}</button>
          ))}
        </div>
      </section>
      {tab === "tax" && <TaxCodes />}
      {tab === "units" && <Units />}
      {tab === "categories" && <Categories />}
      {tab === "groups" && <ItemGroups />}
      {tab === "users" && <Users />}
      {tab === "tags" && <Tags />}
      {tab === "devices" && <Devices />}
      {tab === "company" && <Company />}
    </div>
  );
}

// --- reference lists ------------------------------------------------------------------------

interface TaxCode { id: string; code: string; name: string; rate: string; einvoice_tax_type: string; is_active: boolean; version: number }

function TaxCodes() {
  const { t, can } = useSession();
  const { rows, error, busy, run } = useList<TaxCode>("/tax-codes", paged);
  const [f, setF] = useState({ code: "", name: "", rate: "", einvoice_tax_type: "01" });
  const manage = can("md.setup.manage");
  const add = async () => {
    if (await run(() => api("/tax-codes", { method: "POST", body: { ...f, code: f.code.trim().toUpperCase() } }))) {
      setF({ code: "", name: "", rate: "", einvoice_tax_type: "01" });
    }
  };
  return (
    <section className="card glass">
      <span className="muted">{t("setTaxHint")}</span>
      <ListTable loading={!rows} head={[t("code"), t("name"), t("ratePct"), t("einvoiceType"), ""]}>
        {(rows ?? []).map((r) => (
          <tr key={r.id} className={r.is_active ? "" : "muted"}>
            <td>{r.code}</td><td>{r.name}</td><td className="num">{Number(r.rate)}</td><td>{r.einvoice_tax_type}</td>
            <td className="num">
              {manage && (
                <button className="chip-btn" disabled={busy}
                        onClick={() => run(() => api(`/tax-codes/${r.id}`,
                          { method: "PATCH", body: { version: r.version, is_active: !r.is_active } }))}>
                  {t(r.is_active ? "deactivate" : "activate")}
                </button>
              )}
            </td>
          </tr>
        ))}
      </ListTable>
      {manage && (
        <AddRow busy={busy} onAdd={add} ready={!!f.code.trim() && !!f.name.trim() && f.rate.trim() !== ""}>
          <Inp label={t("code")} value={f.code} max={30} onChange={(x) => setF({ ...f, code: x })} />
          <Inp label={t("name")} value={f.name} max={100} wide onChange={(x) => setF({ ...f, name: x })} />
          <Inp label={t("ratePct")} value={f.rate} numeric onChange={(x) => setF({ ...f, rate: x })} />
          <Inp label={t("einvoiceType")} value={f.einvoice_tax_type} max={2}
               onChange={(x) => setF({ ...f, einvoice_tax_type: x })} />
        </AddRow>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

function Units() {
  const { t, can } = useSession();
  const { rows, error, busy, run } = useList<{ id: string; code: string; name: string; einvoice_code: string }>("/uoms", paged);
  const [f, setF] = useState({ code: "", name: "", einvoice_code: "" });
  const add = async () => {
    if (await run(() => api("/uoms", { method: "POST", body: { ...f, code: f.code.trim().toUpperCase() } }))) {
      setF({ code: "", name: "", einvoice_code: "" });
    }
  };
  return (
    <section className="card glass">
      <span className="muted">{t("setUnitsHint")}</span>
      <ListTable loading={!rows} head={[t("code"), t("name"), t("einvoiceUnit")]}>
        {(rows ?? []).map((r) => <tr key={r.id}><td>{r.code}</td><td>{r.name}</td><td>{r.einvoice_code}</td></tr>)}
      </ListTable>
      {can("md.setup.manage") && (
        <AddRow busy={busy} onAdd={add} ready={!!f.code.trim() && !!f.name.trim() && !!f.einvoice_code.trim()}>
          <Inp label={t("code")} value={f.code} max={10} onChange={(x) => setF({ ...f, code: x })} />
          <Inp label={t("name")} value={f.name} max={50} wide onChange={(x) => setF({ ...f, name: x })} />
          <Inp label={t("einvoiceUnit")} value={f.einvoice_code} max={3}
               onChange={(x) => setF({ ...f, einvoice_code: x })} />
        </AddRow>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

function Categories() {
  const { t, can } = useSession();
  const { rows, error, busy, run } = useList<{ id: string; code: string; name: string }>("/item-categories", paged);
  const [f, setF] = useState({ code: "", name: "" });
  const add = async () => {
    if (await run(() => api("/item-categories", { method: "POST", body: { ...f, code: f.code.trim().toUpperCase() } }))) {
      setF({ code: "", name: "" });
    }
  };
  return (
    <section className="card glass">
      <ListTable loading={!rows} head={[t("code"), t("name")]} empty={t("noResults")}>
        {(rows ?? []).map((r) => <tr key={r.id}><td>{r.code}</td><td>{r.name}</td></tr>)}
      </ListTable>
      {can("md.setup.manage") && (
        <AddRow busy={busy} onAdd={add} ready={!!f.code.trim() && !!f.name.trim()}>
          <Inp label={t("code")} value={f.code} max={30} onChange={(x) => setF({ ...f, code: x })} />
          <Inp label={t("name")} value={f.name} max={100} wide onChange={(x) => setF({ ...f, name: x })} />
        </AddRow>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

const KINDS = ["BRAND", "GROUP", "COLOR", "SIZE", "UDF1", "UDF2", "UDF3"] as const;

/** Brand / group / colour / size / user groups 1-3: the lists an item's grouping picks from. */
function ItemGroups() {
  const { t, can } = useSession();
  const [kind, setKind] = useState<(typeof KINDS)[number]>("BRAND");
  const { rows, error, busy, run } = useList<{ id: string; code: string; name: string; is_active: boolean; version: number }>(
    `/item-attributes?kind=${kind}`, paged);
  const [f, setF] = useState({ code: "", name: "" });
  const add = async () => {
    if (await run(() => api("/item-attributes", {
      method: "POST", body: { kind, code: f.code.trim().toUpperCase(), name: f.name.trim() } }))) {
      setF({ code: "", name: "" });
    }
  };
  const manage = can("md.setup.manage");
  return (
    <section className="card glass">
      <div className="tabs" style={{ margin: 0 }}>
        {KINDS.map((k) => (
          <button key={k} className={k === kind ? "on" : ""} onClick={() => setKind(k)}>{t(`attr${k}` as Key)}</button>
        ))}
      </div>
      <ListTable loading={!rows} head={[t("code"), t("name"), ""]} empty={t("noResults")}>
        {(rows ?? []).map((r) => (
          <tr key={r.id} className={r.is_active ? "" : "muted"}>
            <td>{r.code}</td><td>{r.name}</td>
            <td>
              {manage && (
                <button className="chip-btn" disabled={busy}
                        onClick={() => run(() => api(`/item-attributes/${r.id}`, {
                          method: "PATCH", body: { version: r.version, is_active: !r.is_active } }))}>
                  {t(r.is_active ? "deactivate" : "activate")}
                </button>
              )}
            </td>
          </tr>
        ))}
      </ListTable>
      {manage && (
        <AddRow busy={busy} onAdd={add} ready={!!f.code.trim() && !/\s/.test(f.code.trim()) && !!f.name.trim()}>
          <Inp label={t("code")} value={f.code} max={20} onChange={(x) => setF({ ...f, code: x })} />
          <Inp label={t("name")} value={f.name} max={100} wide onChange={(x) => setF({ ...f, name: x })} />
        </AddRow>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

// --- people -----------------------------------------------------------------------------------

interface TagRow { id: string; code: string; name: string; color: string; is_system: boolean; all_locations: boolean; permissions: string[]; user_count: number }
interface UserRow { id: string; username: string; display_name: string; is_active: boolean; tags: { id: string; name: string; color: string }[]; locations: { code: string }[] }

function Users() {
  const { t, can, me } = useSession();
  const { rows, error, busy, run } = useList<UserRow>("/users", paged);
  const [tags, setTags] = useState<TagRow[]>([]);
  const [f, setF] = useState({ username: "", display_name: "", password: "", tag: "" });
  const manage = can("core.user.manage");
  useEffect(() => {
    api<Page<TagRow>>("/tags").then((p) => setTags(p.items)).catch(() => undefined);
  }, []);
  const add = async () => {
    if (await run(() => api("/users", { method: "POST", body: {
      username: f.username.trim(), display_name: f.display_name.trim(), password: f.password, language: "en",
      tag_ids: f.tag ? [f.tag] : [],
    } }))) setF({ username: "", display_name: "", password: "", tag: "" });
  };
  return (
    <section className="card glass">
      <span className="muted">{t("setUsersHint")}</span>
      <ListTable loading={!rows} head={[t("username"), t("name"), t("setTags"), t("store"), ""]}>
        {(rows ?? []).map((u) => (
          <tr key={u.id} className={u.is_active ? "" : "muted"}>
            <td>{u.username}</td><td>{u.display_name}</td>
            <td>{u.tags.map((x) => (
              <span key={x.id} className="badge" style={{ background: x.color, color: "#fff", marginRight: 4 }}>{x.name}</span>
            ))}</td>
            <td>{u.locations.map((l) => l.code).join(", ") || "—"}</td>
            <td className="num">
              {manage && u.id !== me?.id && (
                <button className="chip-btn" disabled={busy}
                        onClick={() => run(() => api(`/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } }))}>
                  {t(u.is_active ? "deactivate" : "activate")}
                </button>
              )}
            </td>
          </tr>
        ))}
      </ListTable>
      {manage && (
        <AddRow busy={busy} onAdd={add} ready={f.username.trim().length >= 3 && !!f.display_name.trim() && !!f.password}>
          <Inp label={t("username")} value={f.username} max={50} onChange={(x) => setF({ ...f, username: x })} />
          <Inp label={t("name")} value={f.display_name} max={100} onChange={(x) => setF({ ...f, display_name: x })} />
          <Inp label={t("password")} value={f.password} max={200} password onChange={(x) => setF({ ...f, password: x })} />
          <div className="field">
            <label htmlFor="new-user-tag">{t("setTags")}</label>
            <select id="new-user-tag" className="input" value={f.tag} onChange={(e) => setF({ ...f, tag: e.target.value })}>
              <option value="">—</option>
              {tags.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          </div>
        </AddRow>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

function Tags() {
  const { t } = useSession();
  const { rows, error } = useList<TagRow>("/tags", paged);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <section className="card glass">
      <span className="muted">{t("setTagsHint")}</span>
      {!rows ? <span className="muted">{t("loading")}</span> : (
        <ul className="list">
          {rows.map((x) => (
            <li key={x.id} style={{ flexDirection: "column", gap: 6 }}>
              <div className="row">
                <span>
                  <span className="badge" style={{ background: x.color, color: "#fff" }}>{x.name}</span>{" "}
                  <span className="muted">
                    {x.user_count} {t("setUsers").toLowerCase()} · {x.permissions.length} {t("permissions")}
                    {x.all_locations ? ` · ${t("allStores")}` : ""}{x.is_system ? ` · ${t("builtIn")}` : ""}
                  </span>
                </span>
                <button className="chip-btn" onClick={() => setOpen(open === x.id ? null : x.id)}>
                  {t(open === x.id ? "hide" : "showPermissions")}
                </button>
              </div>
              {open === x.id && <div className="perm-list">{x.permissions.sort().map((p) => <code key={p}>{p}</code>)}</div>}
            </li>
          ))}
        </ul>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

interface DeviceRow { id: string; name: string; is_authorized: boolean; last_ip: string | null; last_seen_at: string; is_current: boolean }

function Devices() {
  const { t } = useSession();
  const { rows, error, busy, run } = useList<DeviceRow>("/devices", paged);
  return (
    <section className="card glass">
      <span className="muted">{t("setDevicesHint")}</span>
      <ListTable loading={!rows} head={[t("device"), t("lastSeen"), t("status"), ""]}>
        {(rows ?? []).map((d) => (
          <tr key={d.id}>
            <td>{d.name}{d.is_current && <span className="muted"> · {t("thisComputer")}</span>}</td>
            <td>{d.last_seen_at.slice(0, 16).replace("T", " ")}</td>
            <td><span className={`badge ${d.is_authorized ? "green" : "amber"}`}>{t(d.is_authorized ? "approved" : "waiting")}</span></td>
            <td className="num">
              {!d.is_current && (
                <>
                  <button className="chip-btn" disabled={busy}
                          onClick={() => run(() => api(`/devices/${d.id}/${d.is_authorized ? "deauthorize" : "authorize"}`, { method: "POST" }))}>
                    {t(d.is_authorized ? "block" : "approve")}
                  </button>
                  {d.is_authorized && (
                    <button className="chip-btn" disabled={busy}
                            onClick={() => run(() => api(`/devices/${d.id}/sign-out`, { method: "POST" }))}>
                      {t("signOutDevice")}
                    </button>
                  )}
                </>
              )}
            </td>
          </tr>
        ))}
      </ListTable>
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}

// --- company ----------------------------------------------------------------------------------

const COMPANY_FIELDS: [string, Key, boolean?][] = [
  ["name", "name", true], ["business_activity", "businessActivity", true],
  ["address1", "address1", true], ["address2", "address2", true], ["address3", "address3", true],
  ["postcode", "postcode"], ["city", "city"], ["phone", "phone"], ["email", "email", true],
  ["tin", "tin"], ["id_no", "idNo"], ["sst_no", "sstNo"], ["msic_code", "msic"],
];

function Company() {
  const { t, can } = useSession();
  const [c, setC] = useState<Record<string, unknown> | null>(null);
  const [v, setV] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const manage = can("core.company.manage");
  const take = (x: Record<string, unknown>) => {
    setC(x);
    setV(Object.fromEntries(COMPANY_FIELDS.map(([k]) => [k, x[k] == null ? "" : String(x[k])])));
  };
  useEffect(() => {
    api<Record<string, unknown>>("/company").then(take).catch((e) => setError(errorText(e)));
  }, []);
  const save = async () => {
    if (!c) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const body: Record<string, unknown> = {};
      for (const [k] of COMPANY_FIELDS) {
        const now = v[k].trim() || null;
        if (now !== (c[k] ?? null)) body[k] = now;
      }
      if (Object.keys(body).length === 0) return setNotice(t("nothingChanged"));
      take(await api<Record<string, unknown>>("/company", { method: "PATCH", body: { version: c.version, ...body } }));
      setNotice(t("saved"));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card glass">
      <span className="muted">{t("setCompanyHint")}</span>
      {!c ? <span className="muted">{error ? "" : t("loading")}</span> : (
        <div className="doc-head">
          {COMPANY_FIELDS.map(([k, label, wide]) => (
            <div key={k} className={`field${wide ? " wide" : ""}`}>
              <label htmlFor={`co-${k}`}>{t(label)}</label>
              <input id={`co-${k}`} className="input" value={v[k] ?? ""} disabled={!manage}
                     onChange={(e) => setV({ ...v, [k]: e.target.value })} />
            </div>
          ))}
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
      {notice && !error && <div className="muted" role="status">{notice}</div>}
      {manage && c && (
        <div className="actions"><button className="btn" disabled={busy} onClick={save}>{t("update")}</button></div>
      )}
    </section>
  );
}

// --- small pieces -------------------------------------------------------------------------------

function ListTable({ head, loading, empty, children }: {
  head: string[]; loading: boolean; empty?: string; children: ReactNode;
}) {
  const { t } = useSession();
  if (loading) return <span className="muted">{t("loading")}</span>;
  const none = Array.isArray(children) && children.length === 0;
  return none && empty ? <span className="muted">{empty}</span> : (
    <div className="scroll-x">
      <table className="table one-line">
        <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function AddRow({ children, onAdd, busy, ready }: {
  children: ReactNode; onAdd: () => void; busy: boolean; ready: boolean;
}) {
  const { t } = useSession();
  return (
    <form className="add-row" onSubmit={(e) => { e.preventDefault(); if (ready) onAdd(); }}>
      <div className="doc-head">{children}</div>
      <button className="btn" disabled={busy || !ready}>+ {t("add")}</button>
    </form>
  );
}

function Inp({ label, value, onChange, max, wide, numeric, password }: {
  label: string; value: string; onChange: (v: string) => void; max?: number; wide?: boolean;
  numeric?: boolean; password?: boolean;
}) {
  const id = `inp-${label.replace(/\W+/g, "-")}`;
  return (
    <div className={`field${wide ? " wide" : ""}`}>
      <label htmlFor={id}>{label}</label>
      <input id={id} className="input" value={value} maxLength={max} type={password ? "password" : "text"}
             autoComplete={password ? "new-password" : "off"} inputMode={numeric ? "decimal" : undefined}
             onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
