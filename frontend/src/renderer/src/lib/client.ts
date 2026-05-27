// All API calls go to the local backend, whose URL the Electron preload injects.
const BASE: string =
  (typeof window !== "undefined" && (window as { api?: { backendUrl?: string } }).api?.backendUrl) ||
  "http://127.0.0.1:8765";

export async function jget<T>(url: string): Promise<T> {
  const r = await fetch(BASE + url, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

export async function jpost<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(BASE + url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

export async function jdel<T>(url: string): Promise<T> {
  const r = await fetch(BASE + url, { method: "DELETE", cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

export function downloadUrl(path: string): string {
  return BASE + path;
}

// Backend timestamps are epoch SECONDS (Python time.time()).
export function timeAgo(ts?: number): string {
  if (!ts) return "—";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function duration(start?: number, end?: number): string {
  if (!start) return "—";
  const s = Math.floor((end ?? Date.now() / 1000) - start);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

// "in 5m" / "in 12s" for a future epoch-seconds timestamp (e.g. a scheduled run/wake).
export function untilTime(ts?: number | null): string {
  if (!ts) return "—";
  const s = Math.floor(ts - Date.now() / 1000);
  if (s <= 0) return "now";
  if (s < 60) return `in ${s}s`;
  if (s < 3600) return `in ${Math.floor(s / 60)}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h`;
  return `in ${Math.floor(s / 86400)}d`;
}

export function formatBytes(n?: number): string {
  if (!n) return "empty";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}
