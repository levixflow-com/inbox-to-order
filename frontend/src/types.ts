// Shapes returned by the local API (src/inbox_to_order/web). Every string is shown as text, never as HTML.

export type Lane = "new" | "review" | "ready" | "erp" | "not_order" | "rejected";

export interface Status {
  inbox_mode: "fixture" | "gmail";
  gmail_query: string;
  extractor: "claude" | "rules";
  model: string;
  models: string[];
  ai_ready: boolean;
  needs_ack: boolean;
  mcp_started_seconds: number | null;
}

/** "Read all new" in progress: the step each conversation being read is at, by thread id. */
export interface Batch {
  running: Record<string, string>;
}

export interface ThreadSummary {
  provider: string;
  thread_id: string;
  subject: string;
  sender_email: string;
  last_received_at: string;
  message_count: number;
  attachment_count: number;
  lane: Lane;
  reason: string;
  order: { po_number: string; customer_name: string; lines: number; total_usd: string | null; complete: boolean; erp_order_id: string | null } | null;
}

export interface Evidence {
  message_id: string;
  location: "subject" | "body" | "attachment";
  attachment_id: string | null;
  page: number | null;
  excerpt: string;
}

export interface DraftLine {
  line_no: number;
  customer_item_code: string | null;
  description: string | null;
  raw_quantity_text: string | null;
  sku: string | null;
  product_name: string | null;
  quantity: number | null;
  unit: string | null;
  unit_price: string | null;
  amount_usd: string | null;
  status: "matched" | "needs_review" | "missing" | "not_supported";
  note: string | null;
  evidence: Evidence[];
}

export interface Change {
  field: string;
  line_no: number | null;
  before: string | null;
  after: string | null;
}

export interface Draft {
  draft_id: number;
  customer_id: string;
  customer_name: string;
  po_number: string;
  status: "needs_review" | "ready" | "confirmed" | "rejected";
  version: number;
  ship_to: {
    address_id: string | null;
    one_line: string | null;
    text: string | null;
    status: DraftLine["status"];
    note: string | null;
    evidence: Evidence[];
  };
  lines: DraftLine[];
  issues: { field: string; severity: "blocking" | "info"; message: string }[];
  erp_order_id: string | null;
  erp_version: number | null;
  confirmed_version: number | null;
  latest_change: { version: number; actor: string; changes: Change[]; checked_in: number | null } | null;
  revisions: { version: number; kind: string; actor: string; source_message_id: string | null; changes: string[]; created_at: string }[];
}

export interface Attachment {
  attachment_id: string;
  filename: string;
  size_bytes: number;
  kind: "pdf" | "image" | "other";
  status: string;
  detail: string | null;
  pages: { page: number; text: string }[];
}

export interface Message {
  message_id: string;
  sender_email: string;
  sender_name: string | null;
  subject: string;
  received_at: string;
  body_text: string;
  attachments: Attachment[];
  instruction_like: string[];
  processed: string | null;
}

export interface Step {
  name: string;
  status: "done" | "skipped" | "review" | "failed";
  detail: string;
  seconds: number;
}

export interface Run {
  seconds: number;
  server_start_seconds: number;
  setup_seconds: number;
  error: string | null;
  outcomes: {
    message_id: string;
    outcome: string;
    extractor: string | null;
    draft_id: number | null;
    version: number | null;
    seconds: number;
    error: string | null;
    notices: string[];
    steps: Step[];
    usage: { model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null } | null;
  }[];
  calls: { actor: string; server: string; tool: string; status: string }[];
}

export interface ThreadDetail {
  thread_id: string;
  provider: string;
  subject: string;
  messages: Message[];
  unprocessed: string[];
  lane: Lane;
  reason: string;
  action: "Prepare proposal" | "Analyze update";
  drafts: Draft[];
  held: { message_id: string; issues: string[] }[];
  last_run: Run | null;
}

export interface Product {
  sku: string;
  name: string;
  unit: string;
  unit_label: string;
  customer_item_code: string | null;
  unit_price: string | null;
}

export interface Address {
  address_id: string;
  label: string;
  one_line: string;
  is_default: boolean;
}

export interface ErpOrder {
  order_id: string;
  customer_id: string;
  customer_name: string;
  po_number: string;
  version: number;
  ship_to: Address;
  lines: { line_no: number; sku: string; name: string; quantity: number; unit: string; unit_price: string | null; customer_item_code: string | null }[];
  total_usd: string | null;
  created_at: string;
  updated_at: string;
  history: { version: number; action: string; changes: string[]; source_note: string | null; at: string }[];
}

export type RunEvent =
  | { type: "started"; extractor: string }
  | { type: "step"; message_id: string; step: Step }
  | { type: "done"; run: Run }
  | { type: "error"; code: string; message: string };
