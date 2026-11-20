import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, apiBlob, apiUpload, money } from "../api";
import type { Page } from "../api";
import { useSession } from "../session";
import { DOC_ROUTE, errorText } from "../docs/shared";
import type { Key } from "../i18n";
import { SheetModal } from "./ScanSheet";
import type { Kind, Review, SheetData } from "./ScanSheet";

// Scan intake: supplier papers (invoice, CN, payment advice, bank slip, return note) are
// uploaded or photographed, read by the vision model, checked against the original side by
// side, sorted into their document type, then corrected in a spreadsheet pop-up and turned
// into a DRAFT document - and the next paper in the queue opens straight away.

export interface InboxFile {
  id: string; filename: string; content_type: string; status: string; doc_kind: string | null;
  supplier_id: string | null; doc_no: string | null; total: string | null; location_id: string | null;
  duplicate_of: string | null; linked_doc_type: string | null; linked_doc_id: string | null;
  note: string | null; created_at: string;
}
interface Detail extends InboxFile {
  extraction: { provider: string; model: string | null; status: string; error: string | null; seconds: string | null } | null;
  review: Review | null;
}

export const KINDS: Kind[] = ["INVOICE", "CN", "RETURN_NOTE", "PAYMENT", "BANK"];
const STATUS_BADGE: Record<string, string> = { NEW: "", EXTRACTED: "amber", CONFIRMED: "green", REJECTED: "red" };
const FLAG: Record<string, string> = { TRUSTED: "ok", CHECK: "check", MISSING: "missing" };
// where a confirmed paper's draft opens
const LINKED: Record<string, string> = { ...DOC_ROUTE, BANK: "" };

export function Scan() {
  const { t, can } = useSession();
  const [engine, setEngine] = useState<{ ready: boolean; model: string | null } | null>(null);
  const [queue, setQueue] = useState<InboxFile[] | null>(null);
  const [done, setDone] = useState<InboxFile[]>([]);
  const [showDone, setShowDone] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [image, setImage] = useState<{ url: string; pdf: boolean } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [turn, setTurn] = useState(0);
  const [sheet, setSheet] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; link?: string } | null>(null);
  const [locations, setLocations] = useState<{ id: string; code: string; name: string }[]>([]);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const drop = useRef<HTMLInputElement>(null);
  const allowed = can("pur.doc.create");

  const loadQueue = useCallback(async () => {
    const [q, d] = await Promise.all([
      api<{ items: InboxFile[] }>("/inbox", { query: { queue: true, page_size: 200 } }),
      api<{ items: InboxFile[] }>("/inbox", { query: { page_size: 30 } }),
    ]);
    setQueue(q.items);
    setDone(d.items.filter((f) => f.status === "CONFIRMED" || f.status === "REJECTED"));
    return q.items;
  }, []);

  useEffect(() => {
    api<{ ready: boolean; model: string | null }>("/inbox/engine").then(setEngine).catch(() => setEngine({ ready: false, model: null }));
    api<Page<{ id: string; code: string; name: string }>>("/locations").then((p) => setLocations(p.items)).catch(() => undefined);
    loadQueue().then((q) => { if (q[0]) setPicked(q[0].id); }).catch((e) => { setQueue([]); setError(errorText(e)); });
  }, [loadQueue]);

  // open a paper: its details and the original image
  useEffect(() => {
    setDetail(null); setReview(null); setRejecting(false); setZoom(1); setTurn(0);
    if (!picked) { setImage(null); return; }
    let url: string | null = null;
    api<Detail>(`/inbox/${picked}`).then((d) => { setDetail(d); setReview(d.review); }).catch((e) => setError(errorText(e)));
    apiBlob(`/inbox/${picked}/content`).then((b) => {
      url = URL.createObjectURL(b);
      setImage({ url, pdf: b.type === "application/pdf" });
    }).catch(() => setImage(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [picked]);

  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy("upload"); setError(null);
    let first: string | null = null;
    try {
      for (const file of Array.from(files)) {
        const form = new FormData();
        form.append("file", file);
        form.append("source", /iPhone|Android/i.test(navigator.userAgent) ? "PHONE" : "UPLOAD");
        if (locations[0]) form.append("location_id", locations[0].id);
        const f = await apiUpload<InboxFile & { already_uploaded: boolean }>("/inbox", form);
        first ??= f.id;
        if (f.already_uploaded) setNotice({ text: `${file.name}: ${t("scanAlready")}` });
      }
      await loadQueue();
      if (first) setPicked(first);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
      if (drop.current) drop.current.value = "";
    }
  };

  const recognise = async (id = picked) => {
    if (!id) return;
    setBusy("extract"); setError(null);
    try {
      const d = await api<Detail>(`/inbox/${id}/extract`, { method: "POST", body: {}, timeoutMs: 150000 });
      setDetail(d); setReview(d.review);
      setQueue((q) => q?.map((f) => (f.id === id ? { ...f, ...d } : f)) ?? q);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  /** The person sorts the paper into a type (or corrects the model's guess): checks rerun. */
  const changeKind = async (kind: Kind) => {
    if (!picked) return;
    const data: SheetData = { ...(review?.data ?? {}), kind };
    try {
      setReview(await api<Review>(`/inbox/${picked}/review`, { method: "POST", body: { kind, data } }));
    } catch (e) {
      setError(errorText(e));
    }
  };
  const byHand = () => {
    const kind = (review?.kind as Kind) ?? (detail?.doc_kind as Kind) ?? "INVOICE";
    setReview(review ?? { kind, data: { kind }, fields: {}, findings: [], ready: false });
    setSheet(true);
  };

  const confirmed = async (out: { doc_type: string; doc_id: string }) => {
    setSheet(false);
    const route = LINKED[out.doc_type];
    setNotice({ text: `${t("scanCreated")} ${t(`scanDoc_${out.doc_type}` as Key) ?? out.doc_type}`,
      link: route ? `/docs/${route}/${out.doc_id}` : undefined });
    const q = await loadQueue();
    const next = q.find((f) => f.id !== picked);
    setPicked(next?.id ?? null);
    // keep the rhythm: the next paper is read straight away when a model is set up
    if (next && next.status === "NEW" && engine?.ready) recognise(next.id);
  };

  const reject = async () => {
    if (!picked || reason.trim().length < 3) return;
    try {
      await api(`/inbox/${picked}/reject`, { method: "POST", body: { reason: reason.trim() } });
      const q = await loadQueue();
      setPicked(q[0]?.id ?? null);
      setReason(""); setRejecting(false);
    } catch (e) {
      setError(errorText(e));
    }
  };

  if (!can("pur.doc.read")) return <section className="card glass"><div className="error">{t("noAccess")}</div></section>;
  const d = review?.data;
  const f = review?.fields ?? {};
  const head: { key: string; label: Key; value: string | null | undefined }[] = review ? [
    { key: "supplier", label: "supplier", value: d?.supplier_name as string },
    { key: "doc_no", label: "scanDocNo", value: d?.doc_no as string },
    { key: "doc_date", label: "date", value: d?.doc_date as string },
    { key: "total", label: "total", value: d?.total != null ? money(d.total as string) : null },
  ] : [];
  const open = detail && (detail.status === "NEW" || detail.status === "EXTRACTED");

  return (
    <div className="scan">
      <aside className="card glass scan-queue">
        <div className="row">
          <h2 style={{ margin: 0, fontWeight: 500 }}>{t("scanTitle")}</h2>
          {engine && (
            <span className={`badge ${engine.ready ? "green" : ""}`} title={engine.model ?? ""}>
              {t(engine.ready ? "scanEngineOn" : "scanEngineOff")}
            </span>
          )}
        </div>
        {allowed && (
          <label className={`scan-drop${busy === "upload" ? " busy" : ""}`}
                 onDragOver={(e) => e.preventDefault()}
                 onDrop={(e) => { e.preventDefault(); upload(e.dataTransfer.files); }}>
            <input ref={drop} type="file" multiple accept="image/*,application/pdf" capture="environment"
                   onChange={(e) => upload(e.target.files)} />
            <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2" /><path d="M17 3h2a2 2 0 0 1 2 2v2" /><path d="M21 17v2a2 2 0 0 1-2 2h-2" /><path d="M7 21H5a2 2 0 0 1-2-2v-2" /><path d="M7 12h10" /></svg>
            <span>{busy === "upload" ? t("loading") : t("scanDrop")}</span>
            <span className="muted">{t("scanDropHint")}</span>
          </label>
        )}
        <span className="muted">{t("scanQueue")} · {queue?.length ?? "…"}</span>
        {queue && queue.length === 0 && <span className="muted">{t("scanEmpty")}</span>}
        <ul className="scan-list">
          {(queue ?? []).map((q, i) => (
            <li key={q.id}>
              <button className={q.id === picked ? "on" : ""} onClick={() => setPicked(q.id)}>
                <span className="scan-n">{i + 1}</span>
                <span className="scan-name">
                  <b>{q.doc_no ?? q.filename}</b>
                  <span className="muted">{q.doc_kind ? t(`scanKind_${q.doc_kind}` as Key) : t("scanUnread")}
                    {q.total != null && ` · ${money(q.total)}`}</span>
                </span>
                <span className={`badge ${STATUS_BADGE[q.status]}`}>{t(`scanSt_${q.status}` as Key)}</span>
              </button>
            </li>
          ))}
        </ul>
        <button className="chip-btn" style={{ alignSelf: "flex-start" }} onClick={() => setShowDone((v) => !v)}>
          {t(showDone ? "scanHideDone" : "scanShowDone")}
        </button>
        {showDone && (
          <ul className="scan-list">
            {done.map((q) => (
              <li key={q.id}>
                <button className={q.id === picked ? "on" : ""} onClick={() => setPicked(q.id)}>
                  <span className="scan-name"><b>{q.doc_no ?? q.filename}</b>
                    <span className="muted">{q.doc_kind ? t(`scanKind_${q.doc_kind}` as Key) : ""}</span></span>
                  <span className={`badge ${STATUS_BADGE[q.status]}`}>{t(`scanSt_${q.status}` as Key)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="card glass scan-work">
        {error && <div className="error" role="alert">{error}</div>}
        {notice && (
          <div className="scan-notice" role="status">
            <span>{notice.text}</span>
            {notice.link && <Link to={notice.link}>{t("open")} →</Link>}
            <button className="chip-btn" aria-label={t("close")} onClick={() => setNotice(null)}>✕</button>
          </div>
        )}
        {!picked ? <span className="muted">{t("scanPickOne")}</span> : !detail ? <span className="muted">{t("loading")}</span> : (
          <div className="scan-split">
            <div className="scan-viewer">
              <div className="row" style={{ gap: 6 }}>
                <span className="muted scan-file">{detail.filename}</span>
                <div className="row" style={{ gap: 4 }}>
                  <button className="chip-btn" aria-label={t("zoomOut")} onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}>−</button>
                  <span className="muted">{Math.round(zoom * 100)}%</span>
                  <button className="chip-btn" aria-label={t("zoomIn")} onClick={() => setZoom((z) => Math.min(4, z + 0.25))}>+</button>
                  <button className="chip-btn" aria-label={t("rotate")} onClick={() => setTurn((r) => (r + 90) % 360)}>⟳</button>
                </div>
              </div>
              <div className="scan-image">
                {!image ? <span className="muted">{t("loading")}</span> : image.pdf ? (
                  <iframe title={detail.filename} src={image.url} />
                ) : (
                  <img src={image.url} alt={detail.filename}
                       style={{ transform: `rotate(${turn}deg) scale(${zoom})` }} />
                )}
              </div>
            </div>

            <div className="scan-check">
              <div className="row">
                <span className={`badge ${STATUS_BADGE[detail.status]}`}>{t(`scanSt_${detail.status}` as Key)}</span>
                {detail.extraction?.model && <span className="muted">{detail.extraction.model} · {detail.extraction.seconds}s</span>}
              </div>
              {detail.duplicate_of && <div className="warn-box">{t("scanDuplicate")}</div>}

              {detail.status === "CONFIRMED" ? (
                <div className="flex-col">
                  <span>{t("scanDoneAs")} {t(`scanDoc_${detail.linked_doc_type}` as Key)}</span>
                  {detail.linked_doc_type && LINKED[detail.linked_doc_type] && (
                    <Link className="btn" style={{ textDecoration: "none", alignSelf: "flex-start" }}
                          to={`/docs/${LINKED[detail.linked_doc_type]}/${detail.linked_doc_id}`}>{t("open")} →</Link>
                  )}
                </div>
              ) : detail.status === "REJECTED" ? (
                <span className="muted">{t("scanRejected")}: {detail.note}</span>
              ) : !review ? (
                <div className="flex-col">
                  <span className="muted">{t(engine?.ready ? "scanStep1" : "scanNoEngine")}</span>
                  <div className="actions" style={{ justifyContent: "flex-start" }}>
                    {engine?.ready && allowed && (
                      <button className="btn" disabled={!!busy} onClick={() => recognise()}>
                        {busy === "extract" ? t("scanReading") : t("scanRecognise")}
                      </button>
                    )}
                    {allowed && <button className="btn ghost" disabled={!!busy} onClick={byHand}>{t("scanByHand")}</button>}
                  </div>
                </div>
              ) : (
                <div className="flex-col">
                  <span className="muted">{t("scanStep2")}</span>
                  <div className="field">
                    <label htmlFor="scan-kind">{t("scanKindLabel")}</label>
                    <select id="scan-kind" className="input" value={review.kind} disabled={!allowed}
                            onChange={(e) => changeKind(e.target.value as Kind)}>
                      {KINDS.map((k) => <option key={k} value={k}>{t(`scanKind_${k}` as Key)}</option>)}
                    </select>
                  </div>
                  <dl className="scan-head">
                    {head.map((h) => (
                      <div key={h.key} className={`flag-${FLAG[f[h.key]] ?? "none"}`}>
                        <dt>{t(h.label)}</dt><dd>{h.value || "—"}</dd>
                      </div>
                    ))}
                    {(d?.lines as unknown[] | undefined)?.length !== undefined && (
                      <div><dt>{t("lines")}</dt><dd>{(d?.lines as unknown[]).length}</dd></div>
                    )}
                  </dl>
                  {review.findings.length > 0 && (
                    <ul className="scan-findings">
                      {review.findings.map((x, i) => <li key={i}>{String(x.message)}</li>)}
                    </ul>
                  )}
                  {allowed && (
                    <div className="actions" style={{ justifyContent: "flex-start" }}>
                      <button className="btn" onClick={() => setSheet(true)}>{t("scanOpenSheet")}</button>
                      {engine?.ready && <button className="btn ghost" disabled={!!busy} onClick={() => recognise()}>
                        {busy === "extract" ? t("scanReading") : t("scanReread")}</button>}
                    </div>
                  )}
                </div>
              )}

              {open && allowed && (
                rejecting ? (
                  <div className="row" style={{ gap: 8 }}>
                    <input className="input" style={{ flex: 1 }} value={reason} placeholder={t("scanRejectWhy")}
                           onChange={(e) => setReason(e.target.value)} />
                    <button className="btn danger-fill" disabled={reason.trim().length < 3} onClick={reject}>{t("scanReject")}</button>
                  </div>
                ) : (
                  <button className="btn ghost danger" style={{ alignSelf: "flex-start" }} onClick={() => setRejecting(true)}>
                    {t("scanReject")}
                  </button>
                )
              )}
            </div>
          </div>
        )}
      </section>

      {sheet && detail && review && (
        <SheetModal fileId={detail.id} title={detail.doc_no ?? detail.filename} review={review}
                    locations={locations} locationId={detail.location_id ?? locations[0]?.id ?? ""}
                    imageUrl={image && !image.pdf ? image.url : null}
                    onClose={(latest) => { setSheet(false); if (latest) setReview(latest); }}
                    onConfirmed={confirmed} />
      )}
    </div>
  );
}
