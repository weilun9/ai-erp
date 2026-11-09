// Uploads queued offline sales from any page (not only while the till is open): at start,
// every 15 s, and as soon as the browser says the network is back.

import { useEffect, useState } from "react";
import { outbox } from "./outbox";

const EVENT = "outbox-change";

export function announce() {
  window.dispatchEvent(new Event(EVENT));
}

/** Number of sales waiting to upload; re-renders when it changes. */
export function useWaiting(): number {
  const [n, setN] = useState(() => outbox.list().length);
  useEffect(() => {
    const update = () => setN(outbox.list().length);
    window.addEventListener(EVENT, update);
    window.addEventListener("storage", update); // another tab of this app changed it
    return () => {
      window.removeEventListener(EVENT, update);
      window.removeEventListener("storage", update);
    };
  }, []);
  return n;
}

let running = false;

/** Run once at the app's top level while someone is logged in. */
export function useOutboxSync(enabled: boolean, onRefused: (message: string) => void) {
  useEffect(() => {
    if (!enabled) return;
    const tick = async () => {
      if (running || !outbox.list().length) return;
      running = true;
      try {
        const r = await outbox.flush();
        if (r.failed.length) onRefused(r.failed[0].message);
      } finally {
        running = false;
        announce();
      }
    };
    tick();
    const id = window.setInterval(tick, 15000);
    window.addEventListener("online", tick);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("online", tick);
    };
  }, [enabled, onRefused]);
}
