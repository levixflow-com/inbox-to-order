import { Icon, type IconName } from "./icons";
import { LANE_TITLE, money, plural, when } from "./format";
import type { Batch, Lane, ThreadSummary } from "./types";

// Columns of the board, in the order an email travels. Each says in a few words what it holds.
// Rejected proposals share the last column with mail that is not an order.
const COLUMNS: { lanes: Lane[]; title: string; what: string; icon: IconName; empty: string }[] = [
  { lanes: ["new"], title: LANE_TITLE.new, what: "Arrived, not read yet", icon: "mail", empty: "Nothing new in the inbox." },
  { lanes: ["review"], title: LANE_TITLE.review, what: "A question for you", icon: "question", empty: "No open questions." },
  { lanes: ["ready"], title: LANE_TITLE.ready, what: "Order prepared, check it", icon: "check", empty: "No proposals waiting for you." },
  { lanes: ["erp"], title: LANE_TITLE.erp, what: "Confirmed and written", icon: "erp", empty: "No orders written yet." },
  { lanes: ["not_order", "rejected"], title: "Set aside", what: "Not an order", icon: "archive", empty: "Questions, invoices and other mail land here." },
];

interface Props {
  threads: ThreadSummary[] | null;
  batch: Batch | null;
  moved: string[];
  canRun: boolean;
  demo: boolean;
  onReadAll: () => void;
  onRefresh: () => void;
  onReset: () => void;
}

export function Queue({ threads, batch, moved, canRun, demo, onReadAll, onRefresh, onReset }: Props) {
  if (!threads) return <p className="loading">Reading the inbox through the Mail MCP server…</p>;
  const newCount = threads.filter((t) => t.lane === "new").length;

  return (
    <section aria-label="Inbox queue">
      <div className="bar">
        <h1>Inbox</h1>
        <span className="count">{plural(threads.length, "conversation")}</span>
        <div className="spacer" />
        {demo && (
          <button className="btn ghost" onClick={onReset} disabled={!!batch} title="Deletes all proposals and history, and resets the Demo ERP">
            Reset demo data
          </button>
        )}
        <button className="btn" onClick={onRefresh} disabled={!!batch}>
          Refresh inbox
        </button>
        <button className="btn primary" onClick={onReadAll} disabled={!!batch || newCount === 0 || !canRun} title={canRun ? undefined : "Accept the data-sharing notice first"}>
          {batch ? "Reading…" : `Read all new (${newCount})`}
        </button>
      </div>
      <div className="board">
        {COLUMNS.map((col) => {
          const items = threads.filter((t) => col.lanes.includes(t.lane));
          return (
            <section key={col.title} className={`lane lane-${col.lanes[0]}`} aria-label={col.title}>
              <header className="lanehead">
                <span className="ic">
                  <Icon name={col.icon} />
                </span>
                <h2>{col.title}</h2>
                <span className="big">{items.length}</span>
                <p>{col.what}</p>
              </header>
              {items.length === 0 && <p className="empty">{col.empty}</p>}
              {items.map((t) => (
                <Card key={t.thread_id} t={t} running={batch?.running[t.thread_id] ?? null} moved={moved.includes(t.thread_id)} />
              ))}
            </section>
          );
        })}
      </div>
    </section>
  );
}

function Card({ t, running, moved }: { t: ThreadSummary; running: string | null; moved: boolean }) {
  const o = t.order;
  return (
    <a href={`#/t/${encodeURIComponent(t.thread_id)}`} className={`card${running ? " running" : ""}${moved ? " moved" : ""}`}>
      {o ? (
        <>
          <span className="row1">
            <span className="po">{o.po_number}</span>
            {o.total_usd && (
              <span className="amount" title={o.complete ? "Order total" : "Total of the lines with a price so far"}>
                {money(o.total_usd)}
              </span>
            )}
          </span>
          <span className="who">{o.customer_name}</span>
        </>
      ) : (
        <>
          <span className="subj">{t.subject}</span>
          <span className="who">{t.sender_email}</span>
        </>
      )}
      <span className="meta">
        {when(t.last_received_at)}
        <span aria-hidden="true">·</span>
        {plural(t.message_count, "email")}
        {t.attachment_count > 0 && (
          <>
            <span aria-hidden="true">·</span>
            <Icon name="clip" />
            {t.attachment_count}
          </>
        )}
      </span>
      {running ? <span className="reason reason-run">{running}…</span> : <span className="reason">{t.reason}</span>}
    </a>
  );
}
