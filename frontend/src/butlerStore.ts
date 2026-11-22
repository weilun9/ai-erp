// The AI butler's chat, kept outside the page: a question keeps running (and its answer
// arrives) while the user looks at other pages, and the top bar can show that it is busy.

import { useSyncExternalStore } from "react";
import { ApiError, api, apiUpload } from "./api";

export interface Conversation { id: string; title: string; updated_at: string }
export interface ToolCall { name: string; arguments: unknown }
export interface Message {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_calls: ToolCall[] | null;
}
export interface Action {
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
  result?: { doc_type?: string; doc_id?: string } | null;
  error: string | null;
}

export interface ButlerState {
  convs: Conversation[];
  current: string | null;
  messages: Message[];
  actions: Action[];
  busySince: number | null;  // ms timestamp the running question started
  busyConv: string | null;
  thought: Record<number, number>;  // answer message id -> seconds it took
  error: string | null;
  errorKind: "off" | null;
}

let state: ButlerState = {
  convs: [], current: null, messages: [], actions: [], busySince: null, busyConv: null,
  thought: {}, error: null, errorKind: null,
};
const listeners = new Set<() => void>();
let controller: AbortController | null = null;

function set(patch: Partial<ButlerState> | ((s: ButlerState) => Partial<ButlerState>)) {
  state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
  listeners.forEach((l) => l());
}

export function useButler(): ButlerState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l); },
    () => state,
  );
}

export const butler = {
  loadConvs() {
    return api<Conversation[]>("/ai/conversations").then((convs) => set({ convs })).catch(() => undefined);
  },

  async open(id: string) {
    set({ current: id, error: null, errorKind: null });
    if (state.busyConv === id) return;  // its messages are on screen and still arriving
    const [msgs, pending] = await Promise.all([
      api<Message[]>(`/ai/conversations/${id}/messages`),
      api<Action[]>("/ai/actions"),
    ]);
    if (state.current !== id) return;
    set({ messages: msgs, actions: pending.filter((a) => a.conversation_id === id) });
  },

  newChat() {
    if (state.busySince) return;
    set({ current: null, messages: [], actions: [], error: null, errorKind: null });
  },

  async send(content: string) {
    if (!content.trim() || state.busySince) return;
    const started = Date.now();
    controller = new AbortController();
    const signal = controller.signal;
    set((s) => ({
      busySince: started, error: null, errorKind: null,
      messages: [...s.messages, { id: -started, role: "user", content, tool_calls: null }],
    }));
    let id = state.current;
    try {
      if (!id) {
        id = (await api<Conversation>("/ai/conversations", { method: "POST", body: {} })).id;
        set({ current: id });
      }
      set({ busyConv: id });
      const turn = await api<{ messages: Message[]; pending_actions: Action[]; seconds: number }>(
        `/ai/conversations/${id}/messages`, { method: "POST", body: { content }, signal, timeoutMs: 180000 });
      const answer = [...turn.messages].reverse().find((m) => m.role === "assistant" && !m.tool_calls?.length);
      if (state.current === id) {
        set((s) => ({
          messages: [...s.messages.filter((x) => x.id > 0), ...turn.messages],
          actions: [...s.actions, ...turn.pending_actions],
        }));
      }
      if (answer) set((s) => ({ thought: { ...s.thought, [answer.id]: (Date.now() - started) / 1000 } }));
      butler.loadConvs();
    } catch (e) {
      if (signal.aborted) {
        set((s) => ({ messages: [...s.messages, { id: -Date.now(), role: "assistant", content: "Stopped.", tool_calls: null }] }));
      } else if (e instanceof ApiError && e.details.settings) {
        set({ error: "", errorKind: "off" });
      } else {
        set({ error: e instanceof Error ? e.message : String(e) });
      }
    } finally {
      controller = null;
      set({ busySince: null, busyConv: null });
    }
  },

  /** Stop: the screen stops waiting at once; the server ends the turn after its current step. */
  stop() {
    const id = state.busyConv;
    controller?.abort();
    if (id) api(`/ai/conversations/${id}/stop`, { method: "POST" }).catch(() => undefined);
  },

  /** A filled Excel template attached in the chat: the server checks it and answers with a
   *  card to agree to, or what to fix. */
  async attach(file: File) {
    if (state.busySince) return;
    const started = Date.now();
    set({ busySince: started, error: null, errorKind: null });
    try {
      let id = state.current;
      if (!id) {
        id = (await api<Conversation>("/ai/conversations", { method: "POST", body: {} })).id;
        set({ current: id });
      }
      set({ busyConv: id });
      const form = new FormData();
      form.append("file", file);
      const turn = await apiUpload<{ messages: Message[]; pending_actions: Action[] }>(
        `/ai/conversations/${id}/sheet`, form);
      if (state.current === id) {
        set((s) => ({ messages: [...s.messages, ...turn.messages], actions: [...s.actions, ...turn.pending_actions] }));
      }
      butler.loadConvs();
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
    } finally {
      set({ busySince: null, busyConv: null });
    }
  },

  updateAction(a: Action) {
    set((s) => ({ actions: s.actions.map((x) => (x.id === a.id ? a : x)) }));
  },
};
