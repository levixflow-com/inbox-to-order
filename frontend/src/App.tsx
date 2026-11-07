import { useCallback, useEffect, useState } from "react";
import { api, ApiError, processThread } from "./api";
import { Queue } from "./Queue";
import { Review } from "./Review";
import type { Batch, Status, ThreadSummary } from "./types";
import { Icon } from "./icons";

// How many conversations "Read all new" reads at the same time.
const PARALLEL_READS = 4;

/** "claude-haiku-5-5" -> "Claude Haiku 5.5" */
function modelName(id: string): string {
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(id);
  if (!m) return id;
  return `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ""}`;
}

function threadFromHash(): string | null {
  const m = /^#\/t\/(.+)$/.exec(window.location.hash);
  return m ? decodeURIComponent(m[1]) : null;
}

export function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(threadFromHash());
  const [ack, setAck] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [moved, setMoved] = useState<string[]>([]);

  const fail = useCallback((e: unknown) => {
    setError(e instanceof ApiError ? `${e.message} (${e.code})` : String(e));
  }, []);

  const loadQueue = useCallback(
    async (refresh = false): Promise<ThreadSummary[] | null> => {
      try {
        const q = await api<{ threads: ThreadSummary[] }>(`/api/queue${refresh ? "?refresh=true" : ""}`);
        setThreads(q.threads);
        return q.threads;
      } catch (e) {
        fail(e);
        return null;
      }
    },
    [fail],
  );

  const loadStatus = useCallback(async () => {
    try {
      const s = await api<Status>("/api/status");
      setStatus(s);
      return s;
    } catch (e) {
      fail(e);
      return null;
    }
  }, [fail]);

  useEffect(() => {
    loadStatus().then((s) => {
      if (s) setAck(s.inbox_mode === "fixture");
    });
    loadQueue();
    const onHash = () => setOpenId(threadFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [loadQueue, loadStatus]);

  async function changeSettings(patch: Partial<Pick<Status, "inbox_mode" | "extractor" | "model">>) {
    try {
      const s = await api<Status>("/api/settings", patch);
      setStatus(s);
      if (patch.inbox_mode) {
        setAck(patch.inbox_mode === "fixture");
        setThreads(null);
        window.location.hash = "#/";
        await loadQueue(true);
      }
    } catch (e) {
      fail(e);
    }
  }

  async function readAllNew() {
    if (!threads) return;
    // Longest conversations first: their emails are read one after another, so they set the total time.
    const queue = threads
      .filter((t) => t.lane === "new")
      .sort((a, b) => b.message_count - a.message_count)
      .map((t) => t.thread_id);
    let stop = false;
    const show = (id: string, step: string | null) =>
      setBatch((b) => {
        const running = { ...(b?.running ?? {}) };
        if (step === null) delete running[id];
        else running[id] = step;
        return { running };
      });

    // A run reads one message; a conversation with a later reply is read message by message, oldest first.
    async function readConversation(id: string) {
      for (let round = 0; round < 10 && !stop; round++) {
        show(id, "Starting");
        let failed = false;
        try {
          await processThread(id, ack, (e) => {
            if (e.type === "step") show(id, e.step.name);
            if (e.type === "error") {
              failed = true;
              setError(`${e.message} (${e.code})`);
            }
          });
        } catch (e) {
          fail(e);
          stop = true;
          break;
        }
        const now = await loadQueue();
        if (failed || now?.find((t) => t.thread_id === id)?.lane !== "new") break;
      }
      show(id, null);
      setMoved((m) => [...m.filter((x) => x !== id), id]);
    }

    // Different conversations are read at the same time, a few at once.
    async function worker() {
      for (let id = queue.shift(); id !== undefined && !stop; id = queue.shift()) await readConversation(id);
    }
    setBatch({ running: {} });
    await Promise.all(Array.from({ length: Math.min(PARALLEL_READS, queue.length) }, worker));
    setBatch(null);
    loadStatus();
  }

  async function resetDemo() {
    try {
      await api("/api/reset", {});
      setMoved([]);
      window.location.hash = "#/";
      await loadQueue(true);
    } catch (e) {
      fail(e);
    }
  }

  const canRun = !!status && (!status.needs_ack || ack);

  return (
    <div className="app">
      <header className="top">
        <a className="brand" href="#/">
          <span className="logo">
            <Icon name="inbox" />
          </span>
          Inbox to Order
        </a>
        <span className="tagline">Order emails in, reviewed orders out</span>
        <span className="spacer" />
        {status && (
          <>
            <span className="feed" title="Where the emails come from and who reads them">
              <span className="live" aria-hidden="true" />
              {status.inbox_mode === "gmail" ? "Gmail test inbox, read-only" : "Demo emails"}
              <span className="muted">·</span>
              {status.extractor === "claude" ? `Read by ${modelName(status.model)}` : "Read by rules, no AI"}
            </span>
            <details className="settings">
              <summary className="btn ghost">
                <Icon name="sliders" />
                Settings
              </summary>
              <div className="settings-panel">
                <label className="field">
                  <span>Inbox</span>
                  <select value={status.inbox_mode} onChange={(e) => changeSettings({ inbox_mode: e.target.value as Status["inbox_mode"] })} disabled={!!batch}>
                    <option value="fixture">Demo emails</option>
                    <option value="gmail">Gmail test account (read-only)</option>
                  </select>
                </label>
                <label className="field">
                  <span>Reading</span>
                  <select value={status.extractor} onChange={(e) => changeSettings({ extractor: e.target.value as Status["extractor"] })} disabled={!!batch}>
                    {status.ai_ready && <option value="claude">Claude</option>}
                    <option value="rules">Rules, no AI</option>
                  </select>
                </label>
                {status.extractor === "claude" && (
                  <label className="field">
                    <span>Model</span>
                    <select value={status.model} onChange={(e) => changeSettings({ model: e.target.value })} disabled={!!batch}>
                      {status.models.map((m) => (
                        <option key={m}>{m}</option>
                      ))}
                    </select>
                  </label>
                )}
                {status.needs_ack && ack && (
                  <label className="consent">
                    <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                    <span>Email and PDF text is sent to Anthropic when you read a message: accepted for this test mailbox.</span>
                  </label>
                )}
                <p className="poc">
                  Proof of concept with fictional companies and a fictional order system.{" "}
                  {status.inbox_mode === "gmail" ? `Reads a Gmail test account, read-only (${status.gmail_query}).` : "Reads the demo emails in fixtures/."} Nothing is ever sent, changed or deleted in the mailbox.
                </p>
              </div>
            </details>
            <a className="btn" href="/erp" target="demo-erp" rel="noopener">
              <Icon name="erp" />
              Open Demo ERP
            </a>
          </>
        )}
      </header>

      {status?.needs_ack && !ack && (
        <label className="consent consent-banner">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>
            <b>Before reading:</b> the text of each email you process and of its PDFs is sent to Anthropic for interpretation. Tick to accept; this mailbox holds test messages only.
          </span>
        </label>
      )}

      {error && (
        <div className="toast" role="alert">
          <span>{error}</span>
          <button className="link" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}

      <main>
        {openId ? (
          <Review key={`${status?.inbox_mode}:${openId}`} threadId={openId} status={status} ack={ack} canRun={canRun} onError={fail} onChanged={() => loadQueue()} />
        ) : (
          <Queue
            threads={threads}
            batch={batch}
            moved={moved}
            canRun={canRun}
            demo={status?.inbox_mode === "fixture"}
            onReadAll={readAllNew}
            onRefresh={() => loadQueue(true)}
            onReset={resetDemo}
          />
        )}
      </main>
    </div>
  );
}
