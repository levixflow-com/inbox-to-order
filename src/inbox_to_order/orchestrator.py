"""Application orchestrator: process the next unprocessed message(s) of one email thread.

Read thread (Mail MCP) -> per message: duplicate checks -> read PDF text (Mail MCP; images such as
logos are ignored) -> identify the customer (Demo ERP MCP) -> interpret (Claude via read-only MCP
tools, or the no-AI rules) -> deterministic checks (pipeline.py) -> save a proposal in the app's
own database for the operator. Nothing is written to the Demo ERP here; see review.py.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import Settings, estimate_cost_usd
from .extractors import ExtractionError, Extractor, RuleBasedExtractor
from .mcp_client import Toolbox, ToolCallError, open_toolbox
from .models import AttachmentText, ChangeOut, CustomerLookup, Issue, Provenance, SourceThread
from .pipeline import MessageContext, build_plan
from .services import drafts


@dataclass
class Step:
    name: str
    status: str  # done | skipped | review | failed
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Usage:
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class MessageOutcome:
    message_id: str
    subject: str
    outcome: str = "pending"  # created | amended | replay | already_processed | duplicate | needs_review | no_action | rejected | error | skipped
    extractor: str | None = None
    draft_id: int | None = None
    version: int | None = None
    changes: list[ChangeOut] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    usage: Usage | None = None


@dataclass
class ThreadResult:
    thread_id: str
    provider: str | None = None
    outcomes: list[MessageOutcome] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    server_start_seconds: float = 0.0  # 0 when an already-running MCP session was used


def make_extractor(settings: Settings, conn: sqlite3.Connection | None = None) -> Extractor:
    if settings.resolved_extractor() == "claude":
        from .agent import ClaudeExtractor

        def on_usage(model: str, usage: Any) -> None:
            if conn is not None:
                db.audit(
                    conn,
                    "model",
                    "model_call",
                    model=model,
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                )

        return ClaudeExtractor(settings, on_usage=on_usage)
    return RuleBasedExtractor()


def _audit_hook(conn: sqlite3.Connection, thread_id: str):  # noqa: ANN202
    def hook(actor: str, server: str, tool: str, args: dict[str, Any], status: str) -> None:
        detail: dict[str, Any] = {"tool": tool, "server": server, "status": status}
        for k in ("customer_id", "po_number", "attachment_id", "limit"):
            if k in args:
                detail[k] = args[k]
        if "customer_code_or_description" in args:
            detail["query"] = args["customer_code_or_description"]
        db.audit(conn, actor, "tool_call", thread_id=thread_id, message_id=args.get("message_id"), **detail)

    return hook


async def process_thread(
    settings: Settings,
    thread_id: str,
    *,
    extractor: Extractor | None = None,
    toolbox: Toolbox | None = None,
    max_messages: int | None = None,
    on_step: StepHook | None = None,
) -> ThreadResult:
    """Process unprocessed messages of a thread in order. `max_messages` limits how many new ones (UI: 1 per click).
    `on_step(message_id, step)` is called as each step ends, so a UI can show a run while it is in progress."""
    started = time.perf_counter()
    conn = db.connect(settings.db_path)
    result = ThreadResult(thread_id=thread_id)
    ex = extractor or make_extractor(settings, conn)

    async def run(box: Toolbox) -> None:
        # Several conversations can be read at once over the same MCP session: each run gets its own view of
        # the toolbox, so its tool calls are logged under its own thread.
        hook = _audit_hook(conn, thread_id)
        if dataclasses.is_dataclass(box):
            box = dataclasses.replace(box, on_call=hook)
        else:  # test doubles
            box.on_call = hook
        # Catch inside the MCP client context: anyio would otherwise wrap errors in an ExceptionGroup.
        try:
            await _process(conn, thread_id, ex, box, result, max_messages, on_step)
        except ToolCallError as exc:
            result.error = f"{exc.code}: {exc.message}"
            db.audit(conn, "app", "processing_failed", thread_id=thread_id, error_code=exc.code)

    try:
        if toolbox is not None:
            await run(toolbox)
        else:
            t0 = time.perf_counter()
            async with open_toolbox() as box:
                result.server_start_seconds = time.perf_counter() - t0
                await run(box)
    except OSError as exc:
        result.error = f"MCP_SERVER_START_FAILED: {type(exc).__name__}"
    finally:
        conn.close()
    result.seconds = time.perf_counter() - started
    return result


StepHook = Callable[[str, Step], None]


class _StepLog(list):
    """The step list of one message that reports each step as it is appended."""

    def __init__(self, message_id: str, hook: StepHook) -> None:
        super().__init__()
        self._message_id, self._hook = message_id, hook

    def append(self, step: Step) -> None:
        super().append(step)
        self._hook(self._message_id, step)


class _Timer:
    def __init__(self) -> None:
        self.t = time.perf_counter()

    def lap(self) -> float:
        now = time.perf_counter()
        elapsed, self.t = now - self.t, now
        return elapsed


async def _process(
    conn: sqlite3.Connection,
    thread_id: str,
    extractor: Extractor,
    box: Toolbox,
    result: ThreadResult,
    max_messages: int | None = None,
    on_step: StepHook | None = None,
) -> None:
    timer = _Timer()
    thread = SourceThread.model_validate(await box.call("get_order_thread", {"thread_id": thread_id}))
    read_seconds = timer.lap()
    result.provider = thread.provider
    blocked = False
    handled = 0
    for msg in thread.messages:
        if max_messages is not None and handled >= max_messages:
            break
        if max_messages is not None and db.processed_record(conn, thread.provider, msg.message_id) is not None:
            continue  # in step-by-step mode, skip silently over already processed messages
        handled += 1
        msg_started = time.perf_counter()
        out = MessageOutcome(message_id=msg.message_id, subject=msg.subject, extractor=extractor.name)
        if on_step is not None:
            out.steps = _StepLog(msg.message_id, on_step)
        result.outcomes.append(out)
        try:
            await _process_message(conn, thread, msg, extractor, box, out, blocked, timer, read_seconds)
        finally:
            out.seconds = time.perf_counter() - msg_started + (read_seconds if handled == 1 else 0.0)
        if out.outcome in ("error", "rejected"):
            blocked = True


async def _process_message(conn, thread, msg, extractor, box, out, blocked, timer, read_seconds) -> None:  # noqa: ANN001, PLR0913
    out.steps.append(Step("Read email", "done", f"{len(thread.messages)} message(s) in conversation", read_seconds))
    timer.lap()
    if blocked:
        out.outcome = "skipped"
        out.steps.append(Step("Wait", "skipped", "An earlier message in this conversation must be processed first."))
        return

    prior = db.processed_record(conn, thread.provider, msg.message_id)
    if prior is not None:
        out.outcome, out.draft_id, out.version = "already_processed", prior["draft_id"], prior["version"]
        out.steps.append(
            Step("Check duplicates", "skipped", f"Already processed ({prior['outcome']}); nothing to do.", timer.lap())
        )
        return
    dup = db.find_processed_by_hash(conn, thread.provider, msg.content_hash)
    if dup is not None:
        db.record_processed(
            conn,
            provider=thread.provider,
            message_id=msg.message_id,
            thread_id=thread.thread_id,
            content_hash=msg.content_hash,
            outcome="duplicate",
            draft_id=dup["draft_id"],
            detail={"duplicate_of": dup["message_id"]},
        )
        db.audit(
            conn,
            "app",
            "duplicate_ignored",
            thread_id=thread.thread_id,
            message_id=msg.message_id,
            draft_id=dup["draft_id"],
            duplicate_of=dup["message_id"],
        )
        out.outcome, out.draft_id = "duplicate", dup["draft_id"]
        out.notices.append(
            f"Identical content to message {dup['message_id']}, already processed. No new proposal or revision."
        )
        out.steps.append(Step("Check duplicates", "done", "Duplicate content detected; ignored.", timer.lap()))
        return
    out.steps.append(Step("Check duplicates", "done", "New message", timer.lap()))

    pdfs = [a for a in msg.attachments if a.is_pdf]
    images = [a for a in msg.attachments if a.is_image]
    others = [a for a in msg.attachments if not a.is_pdf and not a.is_image]
    attachments = [
        AttachmentText.model_validate(
            await box.call("get_text_attachment", {"message_id": msg.message_id, "attachment_id": a.attachment_id})
        )
        for a in pdfs
    ]
    parts = [
        f"{a.filename}: {a.status}" + (f" ({len(a.pages)} page(s))" if a.status == "ok" else "") for a in attachments
    ]
    parts += [f"{a.filename}: image, ignored" for a in images]
    parts += [f"{a.filename}: not read (only PDF text is supported)" for a in others]
    status = "done" if all(a.status == "ok" for a in attachments) and not others else "review"
    out.steps.append(
        Step("Read PDF", status if msg.attachments else "skipped", "; ".join(parts) or "No attachments", timer.lap())
    )

    customer = CustomerLookup.model_validate(await box.call("find_customer", {"sender_or_name": msg.sender_email}))
    out.steps.append(
        Step(
            "Identify customer",
            "done" if customer.status == "matched" else "review",
            customer.customer.name if customer.customer else "Sender not in the Demo ERP",
            timer.lap(),
        )
    )
    ctx = MessageContext(
        thread=thread,
        message=msg,
        attachments=attachments,
        customer=customer,
        thread_drafts=drafts.drafts_for_thread(conn, thread.provider, thread.thread_id),
        find_local_draft=lambda cid, po: drafts.find_draft(conn, cid, po),
    )
    usage_before = _usage_snapshot(extractor)
    try:
        extraction = await extractor.extract(ctx, box)
    except ExtractionError as exc:
        out.usage = _usage_delta(extractor, usage_before)
        out.outcome, out.error = "error", str(exc)
        out.steps.append(Step(f"Interpret ({extractor.name})", "failed", str(exc), timer.lap()))
        db.audit(
            conn,
            "app",
            "extraction_failed",
            thread_id=thread.thread_id,
            message_id=msg.message_id,
            extractor=extractor.name,
            reason=str(exc),
        )
        return
    out.usage = _usage_delta(extractor, usage_before)
    out.steps.append(
        Step(
            f"Interpret ({extractor.name})",
            "done",
            f"{extraction.intent}: {len(extraction.lines)} line(s), {len(extraction.amendments)} change(s)",
            timer.lap(),
        )
    )

    prov = Provenance(
        provider=thread.provider,
        message_id=msg.message_id,
        thread_id=thread.thread_id,
        content_hash=msg.content_hash,
        extractor=extractor.name,
    )
    plan = await build_plan(ctx, extraction, box, prov)
    for a in others:
        plan.issues.append(
            Issue(
                field=f"attachment:{a.filename}",
                message=f"Attachment {a.filename} ({a.mime_type}) was not read: only PDF text is supported. Check it manually.",
            )
        )
    if plan.request is not None and others:
        plan.request.issues = plan.request.issues + plan.issues[-len(others) :]
    out.issues, out.notices, out.untrusted = plan.issues, out.notices + plan.notices, plan.untrusted
    blocking = [i for i in plan.issues if i.severity == "blocking"]
    out.steps.append(
        Step(
            "Match catalog & check",
            "review" if blocking else "done",
            f"{len(blocking)} open question(s)" if blocking else "All values verified",
            timer.lap(),
        )
    )

    if plan.request is None:
        db.record_processed(
            conn,
            provider=thread.provider,
            message_id=msg.message_id,
            thread_id=thread.thread_id,
            content_hash=msg.content_hash,
            outcome=plan.outcome_without_request,
            detail={
                "issues": [i.model_dump() for i in plan.issues],
                "notices": out.notices,
                "extractor": extractor.name,
            },
        )
        db.audit(
            conn,
            "app",
            f"message_{plan.outcome_without_request}",
            thread_id=thread.thread_id,
            message_id=msg.message_id,
            issues=len(plan.issues),
        )
        out.outcome = plan.outcome_without_request
        out.steps.append(
            Step(
                "Prepare proposal",
                "skipped",
                "Nothing prepared: needs a person." if out.outcome == "needs_review" else "No change",
                timer.lap(),
            )
        )
        return

    saved = drafts.save_draft(conn, plan.request)
    if not saved.ok:
        out.outcome, out.error = "rejected", f"{saved.error_code}: {saved.error_message}"
        out.steps.append(Step("Prepare proposal", "failed", out.error, timer.lap()))
        return
    out.outcome, out.draft_id, out.version, out.changes = saved.outcome, saved.draft_id, saved.version, saved.changes
    label = {"created": "Proposal created", "amended": "New revision", "replay": "Already saved earlier"}[saved.outcome]
    out.steps.append(
        Step(
            "Prepare proposal",
            "done",
            f"{label} (v{saved.version}, {saved.status}); waiting for the operator",
            timer.lap(),
        )
    )


def _usage_snapshot(extractor: Extractor) -> tuple[int, int, int]:
    u = getattr(extractor, "usage_totals", None)
    return (u["calls"], u["input_tokens"], u["output_tokens"]) if u else (0, 0, 0)


def _usage_delta(extractor: Extractor, before: tuple[int, int, int]) -> Usage | None:
    u = getattr(extractor, "usage_totals", None)
    if not u:
        return None
    calls, inp, outp = u["calls"] - before[0], u["input_tokens"] - before[1], u["output_tokens"] - before[2]
    model = u.get("model", "?")
    return Usage(
        model=model, calls=calls, input_tokens=inp, output_tokens=outp, cost_usd=estimate_cost_usd(model, inp, outp)
    )
