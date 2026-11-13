import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useSession } from "../session";
import { errorText } from "./shared";
import type { Key } from "../i18n";

// LHDN MyInvois e-invoice for one posted sales invoice or credit note: check it the way
// LHDN would, send it, ask for the verdict, cancel within the allowed time. Once LHDN has
// validated it, the UUID and QR link go on the printed document.

export interface Einvoice {
  id: string; status: string; environment: string; uuid: string | null; long_id: string | null;
  share_url: string | null; last_error: string | null; errors: unknown[];
  submitted_at: string | null; validated_at: string | null; cancelled_at: string | null;
  cancel_reason: string | null;
}
interface Preview { problems: Record<string, string>[]; warnings: Record<string, string>[] }

const COLOR: Record<string, string> = {
  VALID: "green", SUBMITTED: "amber", PENDING: "amber", INVALID: "red", FAILED: "red", CANCELLED: "",
};
const say = (x: Record<string, string>) => x.message ?? Object.values(x).join(" · ");

export function EinvoicePanel({ docType, docId, onChange }: {
  docType: "INV" | "CN"; docId: string; onChange: (e: Einvoice | null) => void;
}) {
  const { t, can } = useSession();
  const [e, setE] = useState<Einvoice | null | undefined>(undefined);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState("");
  const submitRight = can("sal.einvoice.submit");

  const take = useCallback((x: Einvoice | null) => {
    setE(x);
    onChange(x);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    setPreview(null);
    setError(null);
    api<Einvoice[]>("/einvoice", { query: { doc_id: docId } })
      // the newest that is not cancelled, else the newest
      .then((rows) => take(rows.find((r) => r.status !== "CANCELLED") ?? rows[0] ?? null))
      .catch(() => take(null));
  }, [docId, take]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const check = () => run(async () => setPreview(await api<Preview>(`/einvoice/preview/${docType}/${docId}`)));
  const send = () => run(async () => {
    const sent = await api<Einvoice>("/einvoice/submit", { method: "POST", body: { doc_type: docType, doc_id: docId } });
    take(sent);
    setPreview(null);
    // LHDN validates a moment after accepting: ask once straight away
    if (sent.status === "SUBMITTED") {
      take(await api<Einvoice>(`/einvoice/${sent.id}/refresh`, { method: "POST" }).catch(() => sent));
    }
  });
  const refresh = () => run(async () => take(await api<Einvoice>(`/einvoice/${e?.id}/refresh`, { method: "POST" })));
  const cancel = () => run(async () => {
    take(await api<Einvoice>(`/einvoice/${e?.id}/cancel`, { method: "POST", body: { reason: reason.trim() } }));
    setCancelling(false);
    setReason("");
  });

  if (e === undefined) return null;
  const live = e && e.status !== "CANCELLED" && e.status !== "INVALID" && e.status !== "FAILED";
  return (
    <section className="card glass no-print">
      <div className="row">
        <h3 style={{ margin: 0 }}>{t("einvTitle")}</h3>
        {e ? <span className={`badge ${COLOR[e.status] ?? ""}`}>{t(`einv${e.status}` as Key)}</span>
          : <span className="badge">{t("einvNone")}</span>}
      </div>
      {e?.environment === "simulated" && <span className="muted">{t("einvSimulated")}</span>}
      {e?.uuid && (
        <dl className="facts">
          <div><dt>UUID</dt><dd>{e.uuid}</dd></div>
          {e.submitted_at && <div><dt>{t("einvSent")}</dt><dd>{e.submitted_at.slice(0, 16).replace("T", " ")}</dd></div>}
          {e.validated_at && <div><dt>{t("einvValidated")}</dt><dd>{e.validated_at.slice(0, 16).replace("T", " ")}</dd></div>}
          {e.cancel_reason && <div><dt>{t("issueCANCELLED")}</dt><dd>{e.cancel_reason}</dd></div>}
        </dl>
      )}
      {e?.last_error && <div className="error">{e.last_error}</div>}
      {e?.status === "VALID" && <span className="muted">{t("einvOnPaper")}</span>}

      {preview && (
        <div className={preview.problems.length ? "warn-box" : "advice"}>
          <b>{preview.problems.length ? t("einvProblems") : t("einvLooksFine")}</b>
          {(preview.problems.length > 0 || preview.warnings.length > 0) && (
            <ul>
              {preview.problems.map((x, i) => <li key={`p${i}`}>{say(x)}</li>)}
              {preview.warnings.map((x, i) => <li key={`w${i}`} className="muted">{say(x)}</li>)}
            </ul>
          )}
        </div>
      )}

      {submitRight && (
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          {!live && <button className="btn ghost" disabled={busy} onClick={check}>{t("einvCheck")}</button>}
          {!live && <button className="btn" disabled={busy} onClick={send}>{t("einvSend")}</button>}
          {e?.status === "SUBMITTED" && <button className="btn ghost" disabled={busy} onClick={refresh}>{t("einvRefresh")}</button>}
          {live && e?.uuid && (
            <button className="btn ghost danger" disabled={busy} onClick={() => setCancelling((x) => !x)}>
              {t("einvCancel")}
            </button>
          )}
        </div>
      )}
      {cancelling && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <input className="input" style={{ flex: 1, minWidth: 220 }} value={reason} maxLength={300}
                 placeholder={t("einvCancelWhy")} aria-label={t("einvCancelWhy")}
                 onChange={(x) => setReason(x.target.value)} />
          <button className="btn danger-fill" disabled={busy || reason.trim().length < 3} onClick={cancel}>
            {t("einvCancel")}
          </button>
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  );
}
