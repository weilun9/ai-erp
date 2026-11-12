import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useSession } from "../session";
import { DOC_ROUTE, errorText } from "../docs/shared";
import type { Key } from "../i18n";

// Problem documents: what the system could not reconcile on its own. Today they come from
// supplier payments (an invoice or credit note on the payment advice that is missing, not
// posted, or for a different amount). Each one says what happened, points at the documents
// involved, suggests the next step, and is closed with a note on how it was settled.

interface Issue {
  id: string; issue_type: string; status: "OPEN" | "RESOLVED" | "CANCELLED"; title: string;
  doc_type: string; doc_id: string; doc_no: string | null;
  party_type: string | null; party_id: string | null;
  details: Record<string, unknown>; created_at: string; resolved_at: string | null;
  resolution: string | null;
}

const STATUSES = ["OPEN", "RESOLVED", "CANCELLED"] as const;
const STATUS_COLOR: Record<string, string> = { OPEN: "red", RESOLVED: "green", CANCELLED: "" };
// issue type -> the advice shown, and the document to key in when one is missing
const ADVICE: Record<string, { text: Key; newKind?: string }> = {
  MISSING_INVOICE: { text: "adviceMISSING_INVOICE", newKind: "grn" },
  MISSING_CN: { text: "adviceMISSING_CN", newKind: "supplier-cn" },
  DOC_NOT_READY: { text: "adviceDOC_NOT_READY" },
  AMOUNT_MISMATCH: { text: "adviceAMOUNT_MISMATCH" },
};
const label = (k: string) => k.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
// amounts arrive as 4-decimal strings ("100.0000"): show money the usual way
const show = (v: unknown) =>
  v !== null && typeof v === "object" ? JSON.stringify(v)
    : typeof v === "string" && /^-?\d+\.\d{3,}$/.test(v) ? Number(v).toFixed(2) : String(v);

export function Issues() {
  const { t, can } = useSession();
  const [params, setParams] = useSearchParams();
  const status = (STATUSES as readonly string[]).includes(params.get("status") ?? "")
    ? (params.get("status") as string) : "OPEN";
  const [issues, setIssues] = useState<Issue[] | null>(null);
  const [parties, setParties] = useState<Record<string, string>>({});
  const [closing, setClosing] = useState<string | null>(null);   // issue being resolved
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setIssues(null);
    api<Issue[]>("/issues", { query: { status } })
      .then(setIssues)
      .catch((e) => { setIssues([]); setError(errorText(e)); });
  }, [status]);
  useEffect(() => {
    setError(null);
    setClosing(null);
    load();
  }, [load]);

  // names for the customers / suppliers the issues are about
  useEffect(() => {
    const missing = [...new Set((issues ?? []).filter((i) => i.party_id && !parties[i.party_id])
      .map((i) => `${i.party_type}|${i.party_id}`))];
    missing.forEach((key) => {
      const [type, id] = key.split("|");
      api<{ name: string }>(`/${partyType(type)}s/${id}`)
        .then((p) => setParties((m) => ({ ...m, [id]: p.name })))
        .catch(() => undefined);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [issues]);

  const resolve = async (i: Issue) => {
    setBusy(true);
    setError(null);
    try {
      await api(`/issues/${i.id}/resolve`, { method: "POST", body: { resolution: note.trim() } });
      setClosing(null);
      setNote("");
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!can("core.issue.read")) {
    return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  }
  return (
    <div className="docform">
      <section className="card glass">
        <div>
          <Link to="/finance" className="muted" style={{ textDecoration: "none" }}>← {t("finance")}</Link>
          <h2 style={{ margin: "4px 0 0" }}>{t("issues")}</h2>
          <span className="muted">{t("issuesIntro")}</span>
        </div>
        <div className="tabs" style={{ margin: 0 }}>
          {STATUSES.map((s) => (
            <button key={s} className={s === status ? "on" : ""}
                    onClick={() => setParams(s === "OPEN" ? {} : { status: s }, { replace: true })}>
              {t(`issue${s}` as Key)}
            </button>
          ))}
        </div>
        {error && <div className="error" role="alert">{error}</div>}
      </section>

      {!issues ? <section className="card glass"><span className="muted">{t("loading")}</span></section>
        : issues.length === 0 ? (
          <section className="card glass">
            <span className={status === "OPEN" ? "badge green" : "muted"} style={{ alignSelf: "flex-start" }}>
              {status === "OPEN" ? t("noIssues") : t("noResults")}
            </span>
          </section>
        ) : issues.map((i) => {
          const advice = ADVICE[i.issue_type];
          const route = DOC_ROUTE[i.doc_type];
          const type = i.party_type ? partyType(i.party_type) : null;
          return (
            <section key={i.id} className="card glass issue">
              <div className="row" style={{ alignItems: "flex-start" }}>
                <div>
                  <strong>{i.title}</strong>
                  <div className="muted">
                    {label(i.issue_type)} · {i.created_at.slice(0, 10)}
                    {i.party_id && parties[i.party_id] ? ` · ${parties[i.party_id]}` : ""}
                  </div>
                </div>
                <span className={`badge ${STATUS_COLOR[i.status]}`}>{t(`issue${i.status}` as Key)}</span>
              </div>

              {Object.keys(i.details).length > 0 && (
                <dl className="facts">
                  {Object.entries(i.details).map(([k, v]) => (
                    <div key={k}><dt>{label(k)}</dt><dd>{show(v)}</dd></div>
                  ))}
                </dl>
              )}

              {i.status === "OPEN" && advice && <div className="advice">{t(advice.text)}</div>}
              {i.resolution && (
                <div className="muted">
                  {t(i.status === "CANCELLED" ? "issueCANCELLED" : "issueRESOLVED")}
                  {i.resolved_at ? ` ${i.resolved_at.slice(0, 10)}` : ""}: {i.resolution}
                </div>
              )}

              <div className="actions" style={{ justifyContent: "flex-start" }}>
                {route && i.doc_no && (
                  <Link className="btn ghost" to={`/docs/${route}/${i.doc_id}`}>{t("openDoc")} {i.doc_no}</Link>
                )}
                {type && i.party_id && can("fin.doc.read") && (
                  <Link className="btn ghost" to={`/accounts?type=${type}&party=${i.party_id}`}>{t("accounts")}</Link>
                )}
                {i.status === "OPEN" && advice?.newKind && can("pur.doc.create") && (
                  <Link className="btn ghost" to={`/docs/${advice.newKind}/new`}>+ {t(advice.newKind as Key)}</Link>
                )}
                {i.status === "OPEN" && can("core.issue.handle") && closing !== i.id && (
                  <button className="btn" onClick={() => { setClosing(i.id); setNote(""); }}>{t("markResolved")}</button>
                )}
              </div>

              {closing === i.id && (
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <input className="input" style={{ flex: 1, minWidth: 220 }} value={note} autoFocus
                         maxLength={500} placeholder={t("resolutionHint")} aria-label={t("resolutionHint")}
                         onChange={(e) => setNote(e.target.value)} />
                  <div className="row" style={{ gap: 8 }}>
                    <button className="btn ghost" onClick={() => setClosing(null)}>{t("cancel")}</button>
                    <button className="btn green" disabled={busy || note.trim().length < 3}
                            onClick={() => resolve(i)}>{t("markResolved")}</button>
                  </div>
                </div>
              )}
            </section>
          );
        })}
    </div>
  );
}

/** The API stores C / S (or the words); the routes use the words. */
function partyType(code: string): "customer" | "supplier" {
  return code === "C" || code === "customer" ? "customer" : "supplier";
}
