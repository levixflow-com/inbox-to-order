import type { RunEvent } from "./types";

// Handed to the page by the local server; other websites cannot read it, so they cannot call the API.
const TOKEN = document.querySelector('meta[name="api-token"]')?.getAttribute("content") ?? "";

export class ApiError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Api-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.code ?? `HTTP_${res.status}`, data.message ?? res.statusText);
  return data as T;
}

/** Same-browser channel: Inbox to Order announces a Demo ERP write, the Demo ERP window refreshes at once. */
export const ERP_CHANNEL = "inbox-to-order-erp";

export function announceErpWrite(): void {
  if (!("BroadcastChannel" in window)) return;
  const ch = new BroadcastChannel(ERP_CHANNEL);
  ch.postMessage({ type: "written" });
  ch.close();
}

/** Runs one message of a conversation; `onEvent` receives each step as it ends, then the result. */
export async function processThread(threadId: string, ack: boolean, onEvent: (e: RunEvent) => void): Promise<void> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/process`, {
    method: "POST",
    headers: { "X-Api-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ ack }),
  });
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(data.code ?? `HTTP_${res.status}`, data.message ?? res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) onEvent(JSON.parse(line) as RunEvent);
    }
  }
}
