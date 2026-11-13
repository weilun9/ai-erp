import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { ApiError, api, money } from "../api";
import { useSession } from "../session";

interface Conversation { id: string; title: string; updated_at: string }
interface ToolCall { name: string; arguments: unknown }
interface Message {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_calls: ToolCall[] | null;
}
interface Action {
  id: string;
  conversation_id: string | null;
  status: string;
  summary: {
    action: string;
    customer?: string;
    supplier?: string;
    store?: string;
    date?: string;
    lines?: { item: string; uom: string; qty: string; unit_price: string }[];
    estimated_subtotal?: string | null;
    checks?: string[];
  };
  error: string | null;
}

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

function ActionCard({ a, onDone }: { a: Action; onDone: (a: Action) => void }) {
  const { t } = useSession();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const s = a.summary;
  async function decide(what: "confirm" | "cancel") {
    setBusy(true);
    setErr(null);
    try {
      onDone(await api<Action>(`/ai/actions/${a.id}/${what}`, { method: "POST" }));
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
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn ghost" disabled={busy} onClick={() => decide("cancel")}>{t("cancel")}</button>
          <button className="btn green" disabled={busy} onClick={() => decide("confirm")}>{t("confirm")}</button>
        </div>
      )}
    </div>
  );
}

export function Butler() {
  const { t } = useSession();
  const [params, setParams] = useSearchParams();
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [actions, setActions] = useState<Action[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const asked = useRef(false);

  const loadConvs = () => api<Conversation[]>("/ai/conversations").then(setConvs);

  async function open(id: string) {
    setCurrent(id);
    setError(null);
    const [msgs, pending] = await Promise.all([
      api<Message[]>(`/ai/conversations/${id}/messages`),
      api<Action[]>("/ai/actions"),
    ]);
    setMessages(msgs);
    setActions(pending.filter((a) => a.conversation_id === id));
  }

  async function send(content: string) {
    if (!content.trim() || busy) return;
    setBusy(true);
    setError(null);
    setText("");
    let id = current;
    setMessages((m) => [...m, { id: -Date.now(), role: "user", content, tool_calls: null }]);
    try {
      if (!id) {
        id = (await api<Conversation>("/ai/conversations", { method: "POST", body: {} })).id;
        setCurrent(id);
      }
      const turn = await api<{ messages: Message[]; pending_actions: Action[] }>(
        `/ai/conversations/${id}/messages`, { method: "POST", body: { content } });
      setMessages((m) => [...m.filter((x) => x.id > 0), ...turn.messages]);
      setActions((a) => [...a, ...turn.pending_actions]);
      loadConvs();
    } catch (e) {
      setError(e instanceof ApiError && e.details.settings ? t("butlerOff")
        : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    loadConvs();
    const ask = params.get("ask");
    if (ask && !asked.current) {
      asked.current = true;
      setParams({}, { replace: true });
      setText(ask);
    }
  }, []);

  useEffect(() => {
    // braces: newer browsers return a Promise here, and React must get no return value
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, actions, busy]);

  return (
    <div className="butler">
      <aside className="chats glass">
        <button className="btn" onClick={() => { setCurrent(null); setMessages([]); setActions([]); }}>
          + {t("newChat")}
        </button>
        <div className="muted" style={{ margin: "8px 4px 0" }}>{t("chats")}</div>
        {convs.map((c) => (
          <button key={c.id} className={c.id === current ? "on" : ""} onClick={() => open(c.id)}
                  title={c.title}>{c.title}</button>
        ))}
      </aside>
      <section className="chat glass">
        <div className="messages">
          {messages.length === 0 && !busy && (
            <div className="center" style={{ minHeight: "40vh", textAlign: "center" }}>
              <div>
                <div className="plus" style={{ margin: "0 auto 12px", width: 64, height: 64,
                  borderRadius: "50%", display: "grid", placeItems: "center", fontSize: 30,
                  background: "rgba(255,255,255,0.08)" }}>✦</div>
                <h2 style={{ margin: 0 }}>{t("askButler")}</h2>
                <p className="muted">{t("askPlaceholder")}</p>
              </div>
            </div>
          )}
          {messages.map((m) => {
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
              </div>
            );
          })}
          {actions.map((a) => (
            <ActionCard key={a.id} a={a}
                        onDone={(n) => setActions((xs) => xs.map((x) => (x.id === n.id ? n : x)))} />
          ))}
          {busy && <div className="msg assistant dots">{t("thinking")} <span>●</span><span>●</span><span>●</span></div>}
          {error && <div className="error">{error}</div>}
          <div ref={bottom} />
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send(text); }}>
          <textarea value={text} placeholder={t("askPlaceholder")} rows={1}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); }
                    }} />
          <button className="round-btn" style={{ width: 48, height: 48 }} disabled={busy}
                  aria-label={t("send")}>➤</button>
        </form>
      </section>
    </div>
  );
}
