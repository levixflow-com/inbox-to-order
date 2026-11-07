import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { announceErpWrite, api, ApiError, processThread } from "./api";
import type { CSSProperties } from "react";
import { FIELD_STATUS, LANE_TITLE, MESSAGE_RESULT, money, nameMessages, OUTCOME_TEXT, plural, secs, when } from "./format";
import { segments, type Mark } from "./highlight";
import { Icon, type IconName } from "./icons";
import { RouteStrip } from "./Route";
import type { Address, Draft, Evidence, Lane, Message, Product, Run, Status, Step, ThreadDetail } from "./types";

// One color per proposal line: the line's pin and the words it came from in the emails share it.
// None of them is green, amber or orange, which already mean ready, question and written.
const LINE_COLORS = ["#2f5bea", "#8b3fd9", "#0f8f86", "#d6336c", "#4256b8", "#a8662a"];
const SHIP_COLOR = "#5b6b7c";
const lineColor = (label: string) => (label === "S" ? SHIP_COLOR : LINE_COLORS[(Number(label) - 1) % LINE_COLORS.length]);
const tint = (label: string) => ({ "--c": lineColor(label) }) as CSSProperties;

const STATUS_ICON: Record<Lane, IconName> = { new: "mail", review: "question", ready: "check", erp: "erp", not_order: "archive", rejected: "archive" };

function initials(name: string): string {
  const parts = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

interface Props {
  threadId: string;
  status: Status | null;
  ack: boolean;
  canRun: boolean;
  onError: (e: unknown) => void;
  onChanged: () => void;
}

type MarkSpec = Mark & { label: string; ev: Evidence };

function evidenceMarks(drafts: Draft[]): MarkSpec[] {
  const out: MarkSpec[] = [];
  for (const d of drafts) {
    for (const ln of d.lines) for (const ev of ln.evidence) out.push({ key: `${d.draft_id}:${ln.line_no}`, label: String(ln.line_no), excerpt: ev.excerpt, ev });
    for (const ev of d.ship_to.evidence) out.push({ key: `${d.draft_id}:S`, label: "S", excerpt: ev.excerpt, ev });
  }
  return out;
}

/** Scroll the source to a value's evidence: the latest one, which is where the current value comes from. */
function reveal(key: string) {
  const all = document.querySelectorAll<HTMLElement>(`mark[data-key="${CSS.escape(key)}"]`);
  const el = all[all.length - 1];
  if (!el) return;
  const details = el.closest("details");
  if (details && !details.open) details.open = true;
  // Scroll only the source column when it scrolls on its own: moving the page would slide the proposal
  // under the pointer and light up another line.
  const pane = el.closest<HTMLElement>(".pane");
  if (pane && pane.scrollHeight > pane.clientHeight && getComputedStyle(pane).overflowY === "auto") {
    const box = pane.getBoundingClientRect();
    const middle = (Math.max(box.top, 0) + Math.min(box.bottom, window.innerHeight)) / 2; // of the visible part
    const delta = el.getBoundingClientRect().top - middle;
    pane.scrollBy({ top: delta, behavior: "smooth" });
  } else {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

export function Review({ threadId, status, ack, canRun, onError, onChanged }: Props) {
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const [liveSteps, setLiveSteps] = useState<Step[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [justWritten, setJustWritten] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api<ThreadDetail>(`/api/threads/${encodeURIComponent(threadId)}`));
      setLoadError(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "NOT_IN_INBOX") setLoadError(e.message);
      else onError(e);
    }
  }, [threadId, onError]);

  useEffect(() => {
    load();
  }, [load]);

  const marks = useMemo(() => (detail ? evidenceMarks(detail.drafts) : []), [detail]);

  if (loadError)
    return (
      <section className="review" aria-label="Review">
        <div className="bar">
          <a className="back" href="#/">
            ← Inbox
          </a>
        </div>
        <p className="notfound">{loadError} Go back to the inbox and open a conversation from the board.</p>
      </section>
    );
  if (!detail) return <p className="loading">Loading the conversation through MCP…</p>;

  const running = liveSteps !== null;
  const run: Run | null = detail.last_run;
  const lastOutcome = run?.outcomes.at(-1) ?? null;
  const routeDraft = detail.drafts.find((d) => d.draft_id === lastOutcome?.draft_id) ?? detail.drafts.at(-1) ?? null;
  const extractor = lastOutcome?.extractor ?? status?.extractor ?? "rules";

  async function start() {
    setLiveSteps([]);
    try {
      await processThread(threadId, ack, (e) => {
        if (e.type === "step") setLiveSteps((s) => [...(s ?? []), e.step]);
        if (e.type === "error") onError(new Error(`${e.message} (${e.code})`));
      });
    } catch (e) {
      onError(e);
    }
    await load();
    setLiveSteps(null);
    onChanged();
  }

  const canStart = detail.unprocessed.length > 0 && canRun && !running;
  const lane: Lane = running ? "new" : detail.lane;
  const [headline, sub] = running ? ["Reading the email…", "Follow each step below. Nothing is written to the Demo ERP until you confirm."] : statusText(detail, lastOutcome?.outcome);
  const hasRoute = running || !!lastOutcome || detail.unprocessed.length > 0;

  return (
    <section className="review" aria-label="Review">
      <div className="bar">
        <a className="back" href="#/">
          <Icon name="back" />
          Inbox
        </a>
        <h1>{detail.subject}</h1>
        <span className={`tag tag-${detail.lane}`}>{LANE_TITLE[detail.lane]}</span>
      </div>

      <div className={`status status-${lane}`}>
        <span className="ic">
          <Icon name={STATUS_ICON[lane]} />
        </span>
        <div className="what">
          <p className="runline">{headline}</p>
          <span className="hint">{sub}</span>
        </div>
        {(running || detail.unprocessed.length > 0) && (
          <div className="runact">
            <button className="btn primary" onClick={start} disabled={!canStart}>
              {running ? "Reading…" : detail.action}
            </button>
            <span className="hint">{!canRun ? "Accept the data-sharing notice above first." : plural(detail.unprocessed.length, "email") + " not read yet"}</span>
          </div>
        )}
        {hasRoute && (
          <details className="howread" key={running ? "running" : "idle"} open={running}>
            <summary>How the email was read{!running && lastOutcome ? `: ${OUTCOME_TEXT[lastOutcome.outcome] ?? lastOutcome.outcome}` : ""}</summary>
            <RouteStrip
              steps={running ? liveSteps : lastOutcome?.steps ?? []}
              running={running}
              extractor={extractor}
              draftStatus={running ? null : routeDraft?.status ?? null}
              erpOrderId={routeDraft?.erp_order_id ?? null}
            />
            {!running && run && lastOutcome && <RunFacts run={run} />}
            {!running && run && run.calls.length > 0 && <Calls run={run} />}
          </details>
        )}
        {!running && run?.error && <p className="err">{run.error}</p>}
        {!running && lastOutcome?.error && <p className="err">{lastOutcome.error}. Nothing was saved for this message; you can retry.</p>}
      </div>

      <div className="split">
        <div className="pane source" aria-label="Source">
          <h2 className="panetitle">
            <Icon name="mail" />
            {plural(detail.messages.length, "email")} from the customer
          </h2>
          {detail.provider === "fixture" && <p className="muted small">Demo data: fictional messages created for testing.</p>}
          {detail.messages.map((m, i) => (
            <MessageCard key={m.message_id} n={i + 1} latest={i > 0 && i === detail.messages.length - 1} m={m} marks={marks} active={active} setActive={setActive} />
          ))}
        </div>

        <div className="pane proposal" aria-label="Proposal">
          <h2 className="panetitle">
            <Icon name="doc" />
            Order proposal, prepared by {extractor === "claude" ? "the AI" : "the rules (no AI)"}
          </h2>
          {detail.held.map((h) => (
            <div key={h.message_id} className="held">
              <b>Not turned into a proposal: a person needs to look at it.</b>
              <ul>
                {h.issues.map((i) => (
                  <li key={i}>{i}</li>
                ))}
              </ul>
            </div>
          ))}
          {detail.drafts.length === 0 && detail.held.length === 0 && (
            <p className="emptyhint">{detail.unprocessed.length ? `No proposal yet. ${detail.action} reads the email and prepares one.` : "No order or change was found in this conversation."}</p>
          )}
          {detail.drafts.map((d) => (
            <DraftCard
              key={d.draft_id}
              d={d}
              messageIds={detail.messages.map((m) => m.message_id)}
              active={active}
              setActive={setActive}
              written={justWritten === d.draft_id}
              onWritten={() => {
                setJustWritten(d.draft_id);
                load();
                onChanged();
              }}
              onSaved={() => {
                load();
                onChanged();
              }}
              onError={onError}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

/** The state of the conversation in one sentence, and what to do next. */
function statusText(detail: ThreadDetail, outcome: string | undefined): [string, string] {
  const d = detail.drafts.at(-1);
  const n = detail.unprocessed.length;
  switch (detail.lane) {
    case "new":
      return [
        n === detail.messages.length ? "Not read yet" : `${plural(n, "new email")} since the last read`,
        `${detail.action} reads ${n === 1 ? "it" : "them"} and prepares an order proposal. Nothing is written to the Demo ERP until you confirm.`,
      ];
    case "review":
      return ["This needs your answer before it can become an order", "The question is in the proposal. Use Edit to choose the missing value."];
    case "ready":
      return [
        outcome === "amended" && d ? `A change to ${d.po_number} is ready: check it and confirm` : `Order ${d?.po_number ?? ""} is ready: check the lines and confirm`,
        "Point at a line to see the words it came from in the emails.",
      ];
    case "erp":
      return [`Confirmed and written to the Demo ERP as ${d?.erp_order_id ?? "an order"}`, "A later email from the customer about this order will prepare a new version."];
    case "rejected":
      return ["Rejected: nothing was written to the Demo ERP", detail.reason];
    default:
      return ["Not an order: nothing to do", detail.reason];
  }
}

function RunFacts({ run }: { run: Run }) {
  const o = run.outcomes.at(-1)!;
  const parts = [`${secs(run.seconds)} total`];
  if (run.server_start_seconds) parts.push(`plus ${secs(run.server_start_seconds)} to start the MCP servers`);
  if (run.setup_seconds) parts.push(`including ${secs(run.setup_seconds)} of setup before the first step`);
  return (
    <p className="facts">
      <span>{parts.join(", ")}</span>
      <span>{plural(run.calls.length, "MCP call")}</span>
      {o.usage && o.usage.calls > 0 && (
        <>
          <span>
            {plural(o.usage.calls, "Claude call")}, {o.usage.input_tokens.toLocaleString("en-US")} tokens in, {o.usage.output_tokens.toLocaleString("en-US")} out
          </span>
          {o.usage.cost_usd !== null && <span>${o.usage.cost_usd.toFixed(4)} at list price</span>}
        </>
      )}
    </p>
  );
}

function Calls({ run }: { run: Run }) {
  const rows = new Map<string, { who: string; server: string; tool: string; result: string; n: number }>();
  for (const c of run.calls) {
    const who = c.actor === "model" ? "Claude" : c.actor === "app" ? "App" : c.actor;
    const result = c.status.startsWith("refused") ? "Refused" : c.status === "ok" ? "OK" : c.status.replace("error:", "");
    const k = `${who}|${c.server}|${c.tool}|${result}`;
    const r = rows.get(k) ?? { who, server: c.server === "-" ? "blocked before MCP" : c.server, tool: c.tool, result, n: 0 };
    r.n += 1;
    rows.set(k, r);
  }
  return (
    <details className="calls">
      <summary>MCP tool calls in this run ({run.calls.length})</summary>
      <table>
        <thead>
          <tr>
            <th>Who</th>
            <th>Server</th>
            <th>Tool</th>
            <th className="num">Calls</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {[...rows.values()].map((r) => (
            <tr key={`${r.who}${r.server}${r.tool}${r.result}`}>
              <td>{r.who}</td>
              <td>{r.server}</td>
              <td className="tool">{r.tool}</td>
              <td className="num">{r.n}</td>
              <td className={r.result === "Refused" ? "refused" : ""}>{r.result}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small muted">Claude may ask only for read-only tools, and each request is checked before it reaches a server. Writing to the Demo ERP happens only when you confirm.</p>
    </details>
  );
}

function Marked({ text, marks, active, setActive }: { text: string; marks: MarkSpec[]; active: string | null; setActive: (k: string | null) => void }) {
  const segs = segments(text, marks);
  const labels = new Map(marks.map((m) => [m.key, m.label]));
  return (
    <>
      {segs.map((s, i) =>
        s.key ? (
          <mark
            key={i}
            data-key={s.key}
            className={active === s.key ? "on" : ""}
            style={tint(labels.get(s.key) ?? "1")}
            onMouseEnter={() => setActive(s.key!)}
            onMouseLeave={() => setActive(null)}
          >
            <span className="pin" aria-label={`Line ${labels.get(s.key)}`}>
              {labels.get(s.key)}
            </span>
            {s.text}
          </mark>
        ) : (
          <Fragment key={i}>{s.text}</Fragment>
        ),
      )}
    </>
  );
}

function MessageCard({ n, latest, m, marks, active, setActive }: { n: number; latest: boolean; m: Message; marks: MarkSpec[]; active: string | null; setActive: (k: string | null) => void }) {
  const body = marks.filter((x) => x.ev.message_id === m.message_id && x.ev.location === "body");
  const subject = marks.filter((x) => x.ev.message_id === m.message_id && x.ev.location === "subject");
  return (
    <article className="msg">
      <header>
        <span className="avatar" aria-hidden="true">
          {initials(m.sender_name || m.sender_email)}
        </span>
        <span className="who">
          <b>{m.sender_name && !m.sender_email.startsWith(`${m.sender_name}@`) ? m.sender_name : m.sender_email}</b>
          <span>{when(m.received_at)}</span>
        </span>
        <span className="right">
          <span className="msgno">
            Email {n}
            {latest ? ", latest" : ""}
          </span>
          <span className={`tag ${m.processed ? "tag-quiet" : "tag-new"}`}>{m.processed ? MESSAGE_RESULT[m.processed] ?? m.processed.replace("_", " ") : "Not read yet"}</span>
        </span>
      </header>
      <p className="msubj">
        <Marked text={m.subject} marks={subject} active={active} setActive={setActive} />
      </p>
      <div className="body">
        <Marked text={m.body_text} marks={body} active={active} setActive={setActive} />
      </div>
      {m.attachments.map((a) => {
        if (a.kind !== "pdf")
          return (
            <p key={a.attachment_id} className="att muted">
              {a.filename}: not read. {a.kind === "image" ? "Images are ignored; only PDF text is read." : "Only PDF text is read."}
            </p>
          );
        const pageMarks = (p: number) => marks.filter((x) => x.ev.message_id === m.message_id && x.ev.attachment_id === a.attachment_id && (x.ev.page ?? 1) === p);
        const hasMarks = a.pages.some((p) => pageMarks(p.page).length > 0);
        return (
          <details key={a.attachment_id} className="att" open={hasMarks}>
            <summary>
              {a.filename} <span className="muted">{a.status === "ok" ? `PDF, text read, ${plural(a.pages.length, "page")}` : `no text read (${a.detail ?? a.status})`}</span>
            </summary>
            {a.pages.map((p) => (
              <div key={p.page} className="page">
                <span className="muted small">Page {p.page}</span>
                <div className="body">
                  <Marked text={p.text} marks={pageMarks(p.page)} active={active} setActive={setActive} />
                </div>
              </div>
            ))}
          </details>
        );
      })}
      {m.instruction_like.length > 0 && (
        <div className="warn">
          Instruction-like text found. It was treated as customer data and ignored:
          <ul>
            {m.instruction_like.map((s) => (
              <li key={s}>“{s}”</li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}

interface DraftProps {
  d: Draft;
  messageIds: string[];
  active: string | null;
  setActive: (k: string | null) => void;
  written: boolean;
  onWritten: () => void;
  onSaved: () => void;
  onError: (e: unknown) => void;
}

function DraftCard({ d, messageIds, active, setActive, written, onWritten, onSaved, onError }: DraftProps) {
  const named = (t: string) => nameMessages(t, messageIds);
  const [editing, setEditing] = useState(false);
  const [blockedNote, setBlockedNote] = useState(false);
  const [busy, setBusy] = useState(false);
  const blocking = d.issues.filter((i) => i.severity === "blocking");
  const total = d.lines.reduce((s, l) => s + (l.amount_usd ? Number(l.amount_usd) : 0), 0);
  const target = d.erp_order_id && d.erp_version ? `update ${d.erp_order_id} (ERP version ${d.erp_version} to ${d.erp_version + 1})` : "create a new order";

  async function confirm() {
    if (d.status !== "ready") {
      setBlockedNote(true);
      return;
    }
    setBusy(true);
    try {
      await api(`/api/drafts/${d.draft_id}/confirm`, { version: d.version });
      announceErpWrite();
      onWritten();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    setBusy(true);
    try {
      await api(`/api/drafts/${d.draft_id}/reject`, { version: d.version });
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  const key = (n: number | string) => `${d.draft_id}:${n}`;
  const hover = (k: string) => ({ onMouseEnter: () => setActive(k), onMouseLeave: () => setActive(null), onClick: () => reveal(k) });

  return (
    <article className={`draft draft-${d.status}`}>
      <header className="dhead">
        <div>
          <h3>
            {d.po_number}
            <span className="tag tag-quiet tag-plain">Version {d.version}</span>
          </h3>
          <span className="cust">{d.customer_name}</span>
        </div>
        {total > 0 && (
          <div className="total-big">
            <b>{money(total)}</b>
            <span>
              {plural(d.lines.length, "line")}, fictional prices
            </span>
          </div>
        )}
      </header>

      {d.latest_change && (
        <div className="change">
          <p className="small">
            What changed in version {d.latest_change.version}, {d.latest_change.actor === "operator" ? "by you" : "from the customer's latest email"}
          </p>
          {d.latest_change.changes.map((c, i) => (
            <p key={i} className="chg">
              <span className="muted">{c.line_no ? `Line ${c.line_no}, ${c.field.replace("_", " ")}` : c.field.replace("_", " ")}</span>
              {c.before && <s>{c.before}</s>}
              {c.before && <span aria-hidden="true">→</span>}
              <b>{c.after ?? "removed"}</b>
            </p>
          ))}
          {d.latest_change.checked_in && <p className="small muted">You checked it again in version {d.latest_change.checked_in} and changed nothing.</p>}
        </div>
      )}

      {blocking.length > 0 && (
        <div className="questions">
          <Icon name="question" />
          <div>
            <p>{blocking.length === 1 ? "Question for you" : "Questions for you"}</p>
            <ul>
            {blocking.map((i) => (
              <li key={i.message}>{i.message}</li>
            ))}
            </ul>
          </div>
        </div>
      )}

      {editing ? (
        <Editor d={d} onCancel={() => setEditing(false)} onSaved={() => (setEditing(false), onSaved())} onError={onError} />
      ) : (
        <>
          <div className="lines" role="table" aria-label="Order lines">
            <div className="lrow lhead" role="row">
              <span>#</span>
              <span>Customer wrote</span>
              <span>Catalog item</span>
              <span className="num">Qty</span>
              <span className="num">Amount</span>
            </div>
            {d.lines.map((ln) => (
              <div
                key={ln.line_no}
                role="row"
                style={tint(String(ln.line_no))}
                className={`lrow${active === key(ln.line_no) ? " on" : ""}${ln.status !== "matched" ? " flag" : ""}`}
                {...hover(key(ln.line_no))}
                title="Show the words this line came from"
              >
                <span>
                  <span className="pin">{ln.line_no}</span>
                </span>
                <span className="wrote">
                  {ln.customer_item_code || ln.description}
                  {ln.raw_quantity_text && <q>{ln.raw_quantity_text}</q>}
                </span>
                <span className="item">
                  {ln.sku ? (
                    <>
                      <b>{ln.sku}</b> {ln.product_name}
                    </>
                  ) : (
                    <span className="flagtext">{FIELD_STATUS[ln.status]}</span>
                  )}
                  {ln.note && <span className={`lnote${ln.note.startsWith("Changed") ? " changed" : ""}`}>{named(ln.note)}</span>}
                </span>
                <span className="num qty">{ln.quantity ? `${ln.quantity} ${ln.unit ?? ""}` : "—"}</span>
                <span className="num">{ln.amount_usd ? money(ln.amount_usd) : ""}</span>
              </div>
            ))}
            <div style={tint("S")} className={`lrow ship${active === key("S") ? " on" : ""}${d.ship_to.status !== "matched" ? " flag" : ""}`} {...hover(key("S"))}>
              <span>
                <span className="pin">S</span>
              </span>
              <span className="wrote">Ship to</span>
              <span className="item">
                {d.ship_to.one_line ?? <span className="flagtext">{FIELD_STATUS[d.ship_to.status]}</span>}
                {d.ship_to.note && <span className="lnote">{named(d.ship_to.note)}</span>}
              </span>
              <span />
              <span className="num total">{total ? money(total) : ""}</span>
            </div>
          </div>
          <p className="lines-hint">The numbered colors match the words in the emails. Amounts use the Demo ERP's fictional prices.</p>
        </>
      )}

      {d.status === "confirmed" ? (
        <div className={`stamp${written ? " animate" : ""}`}>
          <span className="sid">{d.erp_order_id}</span>
          <span>
            <b>Written to the Demo ERP</b>
            <span className="muted block">
              Proposal v{d.confirmed_version ?? d.version} is ERP version {d.erp_version}, after you confirmed.{" "}
              <a href="/erp" target="demo-erp" rel="noopener">
                Open in Demo ERP
              </a>
            </span>
          </span>
        </div>
      ) : d.status === "rejected" ? (
        <p className="rejected">Rejected. Nothing was written to the Demo ERP.</p>
      ) : (
        !editing && (
          <div className="actions">
            <span className="hint" id={`why-${d.draft_id}`}>{blockedNote && d.status !== "ready" ? "Answer the questions first: use Edit to choose the missing values." : `Confirming will ${target}.`}</span>
            <button className="btn" onClick={() => setEditing(true)} disabled={busy}>
              Edit
            </button>
            <button className="btn" onClick={reject} disabled={busy}>
              Reject
            </button>
            <button className={`btn${d.status === "ready" ? " write" : ""}`} onClick={confirm} disabled={busy} aria-describedby={`why-${d.draft_id}`}>
              {busy ? "Writing…" : "Confirm & write to Demo ERP"}
            </button>
          </div>
        )
      )}

      <details className="revs">
        <summary>History ({plural(d.revisions.length, "version")})</summary>
        <ol>
          {d.revisions.map((r) => (
            <li key={r.version}>
              <b>v{r.version}</b> {r.actor === "assistant" ? "assistant" : r.actor === "operator" ? "you" : "Demo ERP"}
              {r.source_message_id && <span className="muted"> {named(`from message ${r.source_message_id}`)}</span>}:{" "}
              {r.kind === "created" ? "created" : r.kind === "copied_from_erp" ? "copied from the Demo ERP order as it is" : r.changes.join("; ") || "questions only"}
              {d.confirmed_version === r.version && <span className="tag tag-confirmed">written to Demo ERP</span>}
            </li>
          ))}
        </ol>
      </details>
    </article>
  );
}

function Editor({ d, onCancel, onSaved, onError }: { d: Draft; onCancel: () => void; onSaved: () => void; onError: (e: unknown) => void }) {
  const [choices, setChoices] = useState<{ products: Product[]; addresses: Address[] } | null>(null);
  const [rows, setRows] = useState(d.lines.map((l) => ({ sku: l.sku ?? "", quantity: l.quantity ?? 1, wrote: [l.customer_item_code || l.description, l.raw_quantity_text].filter(Boolean).join(" ") })));
  const [address, setAddress] = useState(d.ship_to.address_id ?? "");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    api<{ products: Product[]; addresses: Address[] }>(`/api/drafts/${d.draft_id}/choices`)
      .then((c) => {
        setChoices(c);
        if (!address) setAddress((c.addresses.find((a) => a.is_default) ?? c.addresses[0])?.address_id ?? "");
      })
      .catch(onError);
  }, [d.draft_id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!choices) return <p className="loading">Loading the catalog through the Demo ERP MCP server…</p>;

  async function save() {
    const lines = rows.filter((r) => r.sku || r.quantity);
    if (lines.some((r) => !r.sku || !(r.quantity >= 1))) {
      setProblem("Every line needs a catalog item and a quantity of at least 1.");
      return;
    }
    try {
      await api(`/api/drafts/${d.draft_id}/edit`, { version: d.version, address_id: address, lines: lines.map((r) => ({ sku: r.sku, quantity: r.quantity })), note });
      onSaved();
    } catch (e) {
      onError(e);
    }
  }

  return (
    <div className="editor">
      {rows.map((r, i) => (
        <div key={i} className="erow">
          <span className="n">{i + 1}</span>
          <select aria-label={`Line ${i + 1} catalog item`} value={r.sku} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, sku: e.target.value } : x)))}>
            <option value="">Choose a catalog item</option>
            {choices.products.map((p) => (
              <option key={p.sku} value={p.sku}>
                {p.sku}, {p.name} ({p.unit_label})
              </option>
            ))}
          </select>
          <input aria-label={`Line ${i + 1} quantity`} type="number" min={1} max={10000} step={1} value={r.quantity} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, quantity: Number(e.target.value) } : x)))} />
          <button className="link" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
            Remove
          </button>
          {r.wrote && <span className="muted small wrote">Customer wrote: {r.wrote}</span>}
        </div>
      ))}
      <button className="link" onClick={() => setRows([...rows, { sku: "", quantity: 1, wrote: "" }])}>
        Add a line
      </button>
      <label className="field">
        <span>Ship to</span>
        <select value={address} onChange={(e) => setAddress(e.target.value)}>
          {choices.addresses.map((a) => (
            <option key={a.address_id} value={a.address_id}>
              {a.one_line}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Note (optional)</span>
        <input value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} placeholder="Customer confirmed by phone" />
      </label>
      {problem && <p className="err">{problem}</p>}
      <div className="actions">
        <span className="hint">Saving creates a new version by you. It does not write to the Demo ERP.</span>
        <button className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button className="btn primary" onClick={save}>
          Save corrections
        </button>
      </div>
    </div>
  );
}
