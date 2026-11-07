export function secs(v: number): string {
  return v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(1)} s`;
}

export function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function money(v: string | number): string {
  return `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export const LANE_TITLE: Record<string, string> = {
  new: "New",
  review: "Needs a person",
  ready: "Ready to confirm",
  erp: "In Demo ERP",
  not_order: "Not an order",
  rejected: "Rejected",
};

export const FIELD_STATUS: Record<string, string> = {
  matched: "Matched",
  needs_review: "Needs review",
  missing: "Missing",
  not_supported: "Not supported",
};

/** What reading one message did, as a short badge next to it in the source. */
export const MESSAGE_RESULT: Record<string, string> = {
  created: "Created the proposal",
  amended: "Changed the proposal",
  needs_review: "Held for a person",
  duplicate: "Duplicate",
  no_action: "Not an order",
  error: "Failed, nothing saved",
  rejected: "Not saved",
};

/** Replace raw mailbox ids ("message 1a1b1c89f9c590e2") with "email 2", the number shown in the source. */
export function nameMessages(text: string, ids: string[]): string {
  return ids.reduce((t, id, i) => t.split(`message ${id}`).join(`email ${i + 1}`), text);
}

export const OUTCOME_TEXT: Record<string, string> = {
  created: "New order: a proposal is ready for review",
  amended: "Change to an order: a new proposal version is ready",
  needs_review: "A person needs to look at this message",
  duplicate: "Duplicate of a message already read",
  already_processed: "Already processed: nothing changed",
  replay: "Already processed: nothing changed",
  no_action: "Not an order or a change: no proposal",
  error: "The run failed: nothing was saved",
  rejected: "The proposal could not be saved",
};
