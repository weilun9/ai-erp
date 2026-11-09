// Thin client for the backend. The access token lives only in memory; the remembered
// device keeps a session cookie (HttpOnly, path /api/v1/auth) that /auth/refresh turns
// into a new access token, so a reload or an expired token never logs the user out.

const BASE = "/api/v1";
let token: string | null = null;

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  constructor(status: number, code: string, message: string, details: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function parse(res: Response): Promise<unknown> {
  if (res.status === 204) return null;
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // not JSON (e.g. a proxy's 502 page): the status code still tells what happened
  }
  if (!res.ok) {
    const e = body?.error ?? {};
    throw new ApiError(res.status, e.code ?? "HTTP_" + res.status, e.message ?? res.statusText,
      e.details ?? {});
  }
  return body;
}

export async function refresh(): Promise<boolean> {
  const res = await fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "same-origin" });
  if (!res.ok) {
    token = null;
    return false;
  }
  token = ((await res.json()) as { access_token: string }).access_token;
  return true;
}

export async function login(username: string, password: string): Promise<void> {
  const res = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, remember: true }),
  });
  token = ((await parse(res)) as { access_token: string }).access_token;
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/auth/logout`, { method: "POST", credentials: "same-origin" });
  token = null;
}

type Query = Record<string, string | number | boolean | undefined | null>;

export async function api<T>(
  path: string,
  opts: { method?: string; body?: unknown; query?: Query } = {},
): Promise<T> {
  const qs = opts.query
    ? "?" + new URLSearchParams(
        Object.entries(opts.query)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)]),
      )
    : "";
  const send = () =>
    fetch(`${BASE}${path}${qs}`, {
      method: opts.method ?? "GET",
      credentials: "same-origin",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  let res = await send();
  if (res.status === 401 && (await refresh())) res = await send();
  return (await parse(res)) as T;
}

// --- shapes the screens use ---------------------------------------------------------------

export interface Me {
  id: string;
  username: string;
  display_name: string;
  language: string;
  tags: { code: string; name: string; color: string }[];
  permissions: string[];
  all_locations: boolean;
  locations: { id: string; code: string; name: string }[];
  device: { id: string; name: string };
}

export interface Page<T> {
  items: T[];
  total: number;
}

export const money = (v: string | number | null | undefined) =>
  v === null || v === undefined
    ? "—"
    : "RM " + Number(v).toLocaleString("en-MY", { minimumFractionDigits: 2,
                                                  maximumFractionDigits: 2 });

export const qty = (v: string | number) =>
  Number(v).toLocaleString("en-MY", { maximumFractionDigits: 2 });
