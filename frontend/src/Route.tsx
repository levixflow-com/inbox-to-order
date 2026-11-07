import type { Step } from "./types";
import { secs } from "./format";

// The route of one email, from the inbox to the Demo ERP. Stations light up as the run reports each step.
const STATIONS = [
  { key: "Read email", label: "Email", via: "Mail MCP", tone: "read" },
  { key: "Check duplicates", label: "Duplicates", via: "App", tone: "" },
  { key: "Read PDF", label: "PDF text", via: "Mail MCP", tone: "read" },
  { key: "Identify customer", label: "Customer", via: "Demo ERP MCP", tone: "read" },
  { key: "Interpret", label: "Interpret", via: "", tone: "" },
  { key: "Match catalog & check", label: "Checks", via: "Demo ERP MCP", tone: "read" },
  { key: "Prepare proposal", label: "Proposal", via: "App", tone: "" },
  { key: "Operator", label: "Operator", via: "You", tone: "" },
  { key: "Demo ERP", label: "Demo ERP", via: "Write via MCP", tone: "write" },
] as const;

interface Props {
  steps: Step[];
  running: boolean;
  extractor: string;
  draftStatus: string | null;
  erpOrderId: string | null;
}

export function RouteStrip({ steps, running, extractor, draftStatus, erpOrderId }: Props) {
  const byKey = new Map<string, Step>();
  for (const s of steps) byKey.set(s.name.startsWith("Interpret") ? "Interpret" : s.name, s);
  const stopped = steps.some((s) => s.status === "failed") || steps.some((s) => s.name === "Prepare proposal");
  const nextKey = running && !stopped ? STATIONS.find((s) => s.key !== "Operator" && s.key !== "Demo ERP" && !byKey.has(s.key))?.key : undefined;

  return (
    <ol className="route" style={{ ["--n" as string]: STATIONS.length }} aria-label="Route of this email">
      {STATIONS.map((st) => {
        let state = "idle";
        let value = "";
        let label: string = st.label;
        let via: string = st.via;
        if (st.key === "Interpret") {
          label = extractor === "claude" ? "Claude" : "Rules";
          via = extractor === "claude" ? "Anthropic API" : "No AI";
        }
        const s = byKey.get(st.key);
        if (st.key === "Operator") {
          state = ({ confirmed: "done", ready: "waiting", needs_review: "review", rejected: "failed" } as Record<string, string>)[draftStatus ?? ""] ?? "idle";
          if (running) state = "idle";
        } else if (st.key === "Demo ERP") {
          state = !running && draftStatus === "confirmed" && erpOrderId ? "written" : "idle";
          value = state === "written" ? erpOrderId ?? "" : "";
        } else if (s) {
          state = s.status;
          value = s.status === "skipped" ? "skipped" : secs(s.seconds);
        } else if (st.key === nextKey) {
          state = "active";
          value = "working";
        }
        return (
          <li key={st.key} className={`st st-${state}`} title={s?.detail || undefined}>
            <span className="dot" aria-hidden="true" />
            <span className="nm">{label}</span>
            <span className="ms">{value}</span>
            <span className={`via via-${st.tone}`}>{via}</span>
          </li>
        );
      })}
    </ol>
  );
}
