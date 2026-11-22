import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { api, money } from "../api";
import { useSession } from "../session";
import { butler, useButler } from "../butlerStore";
import type { Action } from "../butlerStore";

// The model's answers are Markdown (tables, bold); rendered, then sanitized.
function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}

const TOOL_LABEL: Record<string, string> = {
  search_items: "items",
  get_stock_balance: "stock",
  find_parties: "parties",
  list_documents: "documents",
  get_document: "document",
  get_open_items: "owed",
  run_report: "report",
  list_issues: "issues",
  list_stores: "stores",
  get_my_shift: "shift",
  search_knowledge: "knowledge",
  draft_document: "draft",
};

/** Seconds since `since`, ticking ten times a second while it runs. */
export function useElapsed(since: number | null): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!since) return;
    const h = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(h);
  }, [since]);
  return since ? Math.max(0, (now - since) / 1000) : 0;
}

function ActionCard({ a }: { a: Action }) {
  const { t } = useSession();
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const s = a.summary;
  async function decide(what: "confirm" | "cancel") {
    setBusy(true);
    setErr(null);
    try {
      const done = await api<Action>(`/ai/actions/${a.id}/${what}`, { method: "POST" });
      butler.updateAction(done);
      // the draft is made: open it, where one more Agree saves and posts it
      if (what === "confirm" && done.status === "CONFIRMED" && done.result?.doc_id) {
        nav(`/docs/${done.result.doc_type}/${done.result.doc_id}?ai=1`);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const label: Record<string, string> = {
    CONFIRMED: t("confirmed"), CANCELLED: t("cancelled"), EXPIRED: t("expired"), FAILED: t("failed"),
  };
  return (
    <div className="action-card">
      <div className="row">
        <strong>{s.action}</strong>
        {a.status !== "PENDING" && (
          <span className={`badge ${a.status === "CONFIRMED" ? "green" : "amber"}`}>
            {label[a.status] ?? a.status}
          </span>
        )}
      </div>
      <div className="muted">{[s.customer ?? s.supplier, s.store, s.date].filter(Boolean).join(" · ")}</div>
      <ul className="list">
        {s.lines?.map((l, i) => (
          <li key={i}><span>{l.item}</span><span>{l.qty} {l.uom} · {l.unit_price}</span></li>
        ))}
      </ul>
      {s.estimated_subtotal && (
        <div className="row"><span className="muted">{t("estimate")}</span>
          <strong>{money(s.estimated_subtotal)}</strong></div>
      )}
      {s.checks && s.checks.length > 0 && (
        <div className="checks"><strong>{t("checks")}</strong>
          {s.checks.map((c) => <div key={c}>• {c}</div>)}</div>
      )}
      {a.error && <div className="error">{a.error}</div>}
      {err && <div className="error">{err}</div>}
      {a.status === "PENDING" && (
        <>
          <span className="muted small">{t("aiAgreeHint")}</span>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn ghost" disabled={busy} onClick={() => decide("cancel")}>{t("cancel")}</button>
            <button className="btn green" disabled={busy} onClick={() => decide("confirm")}>{t("aiAgreeOpen")}</button>
          </div>
        </>
      )}
      {a.status === "CONFIRMED" && a.result?.doc_id && (
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={() => nav(`/docs/${a.result!.doc_type}/${a.result!.doc_id}?ai=1`)}>
            {t("aiOpenDoc")}
          </button>
        </div>
      )}
    </div>
  );
}

export function Butler() {
  const { t } = useSession();
  const [params, setParams] = useSearchParams();
  const st = useButler();
  const [text, setText] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const busy = st.busySince !== null;
  const elapsed = useElapsed(st.busySince);

  useEffect(() => {
    butler.loadConvs();
    const ask = params.get("ask");
    if (ask) {
      setParams({}, { replace: true });
      setText(ask);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // braces: newer browsers return a Promise here, and React must get no return value
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [st.messages, st.actions, busy]);

  const send = () => {
    if (!text.trim() || busy) return;
    const content = text;
    setText("");
    butler.send(content);
  };

  return (
    <div className="butler">
      <aside className="chats glass">
        <button className="btn" disabled={busy} onClick={() => butler.newChat()}>+ {t("newChat")}</button>
        <div className="muted" style={{ margin: "8px 4px 0" }}>{t("chats")}</div>
        {st.convs.map((c) => (
          <button key={c.id} className={c.id === st.current ? "on" : ""} onClick={() => butler.open(c.id)}
                  title={c.title}>
            {c.id === st.busyConv && <span className="think-dot" aria-hidden="true" />}{c.title}
          </button>
        ))}
      </aside>
      <section className="chat glass">
        <div className="messages">
          {st.messages.length === 0 && !busy && (
            <div className="center" style={{ minHeight: "40vh", textAlign: "center" }}>
              <div>
                <div className="plus" style={{ margin: "0 auto 12px", width: 64, height: 64,
                  borderRadius: "50%", display: "grid", placeItems: "center", fontSize: 30,
                  background: "var(--fill)" }}>✦</div>
                <h2 style={{ margin: 0 }}>{t("askButler")}</h2>
                <p className="muted">{t("askPlaceholder")}</p>
              </div>
            </div>
          )}
          {st.messages.map((m) => {
            if (m.role === "tool") return null;
            if (m.role === "assistant" && m.tool_calls?.length) {
              return (
                <div key={m.id} className="tools">
                  {m.tool_calls.map((c, i) => (
                    <span key={i}>✓ {t("usedTools")} {TOOL_LABEL[c.name] ?? c.name}</span>
                  ))}
                </div>
              );
            }
            return (
              <div key={m.id} className={`msg ${m.role}`}>
                {m.role === "assistant" ? <Markdown text={m.content ?? ""} /> : m.content}
                {m.role === "assistant" && st.thought[m.id] !== undefined && (
                  <div className="thought">{t("aiThoughtFor").replace("{s}", st.thought[m.id].toFixed(1))}</div>
                )}
              </div>
            );
          })}
          {st.actions.map((a) => <ActionCard key={a.id} a={a} />)}
          {busy && (st.busyConv === st.current || st.busyConv === null) && (
            <div className="msg assistant dots">
              {t("thinking")} <span>●</span><span>●</span><span>●</span>
              <span className="think-time">{elapsed.toFixed(1)}s</span>
            </div>
          )}
          {st.errorKind === "off" && <div className="error">{t("butlerOff")}</div>}
          {st.error && <div className="error">{st.error}</div>}
          <div ref={bottom} />
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <textarea value={text} placeholder={t("askPlaceholder")} rows={1}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
                    }} />
          {busy ? (
            <button type="button" className="round-btn stop-btn" style={{ width: 48, height: 48 }}
                    aria-label={t("aiStop")} title={t("aiStop")} onClick={() => butler.stop()}>■</button>
          ) : (
            <button className="round-btn" style={{ width: 48, height: 48 }} aria-label={t("send")}>➤</button>
          )}
        </form>
      </section>
    </div>
  );
}
