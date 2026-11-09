// Sales rung up while the network is down wait here and are sent when it is back.
// Each keeps its client_uuid and sold_at, so the server records it once, at the time it
// really happened (a retry of something that did arrive just returns the first result).

import { ApiError, api } from "../api";

const KEY = "pos-outbox";

export interface Pending {
  path: string;
  body: Record<string, unknown> & { client_uuid: string };
  queued_at: string;
}

function read(): Pending[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "[]") as Pending[];
  } catch {
    return [];
  }
}

function write(items: Pending[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(items));
  } catch {
    /* storage full or blocked: the sale stays on screen with a warning */
  }
}

/** No connection, or a proxy in front of the server saying it is unreachable. */
export const isOffline = (e: unknown) =>
  e instanceof TypeError || (e instanceof ApiError && [502, 503, 504].includes(e.status));

export const outbox = {
  list: read,
  push(p: Omit<Pending, "queued_at">) {
    write([...read(), { ...p, queued_at: new Date().toISOString() }]);
  },
  /** Send what is waiting, oldest first. Stops at the first network failure. */
  async flush(): Promise<{ sent: number; failed: { body: Pending; message: string }[] }> {
    let sent = 0;
    const failed: { body: Pending; message: string }[] = [];
    const keep: Pending[] = [];
    const items = read();
    for (let i = 0; i < items.length; i++) {
      try {
        await api(items[i].path, { method: "POST", body: items[i].body });
        sent++;
      } catch (e) {
        if (isOffline(e)) {  // still offline: keep this and the rest
          keep.push(...items.slice(i));
          break;
        }
        // the server refused it (e.g. shift closed): needs a person; keep it visible
        failed.push({ body: items[i], message: e instanceof Error ? e.message : String(e) });
        keep.push(items[i]);
      }
    }
    write(keep);
    return { sent, failed };
  },
  drop(client_uuid: string) {
    write(read().filter((x) => x.body.client_uuid !== client_uuid));
  },
};

