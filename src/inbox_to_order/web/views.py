"""Plain data for the web UI, built from the app's own database and from what the MCP servers returned.

No HTML here: the browser renders every string as text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from .. import db
from ..models import AttachmentText, DraftOut, SourceThread
from ..orchestrator import ThreadResult
from ..services import drafts as drafts_svc
from ..services.erp import line_amount
from ..services.injection import find_instruction_like

# Lanes of the queue, in display order. A lane follows from the data; nobody moves cards by hand.
LANES = ("new", "review", "ready", "erp", "not_order", "rejected")


NOT_A_VALUE = ("draft", "review")  # bookkeeping entries: a save that changed no value is "review"


def describe_change(c: dict[str, Any]) -> str:
    if c.get("field") == "review":
        return "checked, no value changed"
    where = f"Line {c['line_no']} " if c.get("line_no") else ""
    before = f"{c['before']} to " if c.get("before") else ""
    return f"{where}{c['field'].replace('_', ' ')}: {before}{c.get('after') or 'removed'}"


def latest_change(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any] | None:
    """The most recent revision that changed a value (a save without changes is skipped), back to creation.

    `checked_in` is the last later version saved by the operator without changing anything, if any.
    """
    checked_in = None
    for rev in reversed(db.list_revisions(conn, draft_id)):
        if rev["kind"] in ("created", "copied_from_erp"):
            return None
        changes = [c for c in json.loads(rev["changes"]) if c.get("field") not in NOT_A_VALUE]
        if changes:
            return {"version": rev["version"], "actor": rev["actor"], "changes": changes, "checked_in": checked_in}
        if checked_in is None and rev["actor"] == "operator":
            checked_in = rev["version"]
    return None


def lane_for(conn: sqlite3.Connection, provider: str, thread_id: str, message_count: int) -> tuple[str, str]:
    """(lane, reason): the reason is one plain sentence saying why the conversation is in that lane."""
    rows = db.list_processed(conn, thread_id)
    if not rows:
        return "new", "Not read yet"
    pending = message_count - len(rows)
    if pending > 0:
        return "new", f"{pending} new message{'s' if pending > 1 else ''} since the last run"
    found = drafts_svc.drafts_for_thread(conn, provider, thread_id)
    if found:
        d = found[-1]
        if d.status == "needs_review":
            blocking = [i for i in d.issues if i.severity == "blocking"]
            return "review", blocking[0].message if blocking else "Open questions in the proposal"
        if d.status == "confirmed":
            return "erp", f"{d.erp_order_id}, ERP version {d.erp_version}"
        if d.status == "rejected":
            return "rejected", "Rejected by the operator"
        ch = latest_change(conn, d.draft_id)
        if ch:
            return "ready", describe_change(ch["changes"][0]) + (
                f" and {len(ch['changes']) - 1} more" if len(ch["changes"]) > 1 else ""
            )
        return "ready", f"New order, {len(d.lines)} line{'s' if len(d.lines) != 1 else ''}"
    details = [json.loads(r["detail"] or "{}") for r in rows]
    if any(r["outcome"] == "needs_review" for r in rows):
        issues = [i for d in details for i in d.get("issues", [])]
        return "review", issues[0]["message"] if issues else "A person needs to look at this message"
    notes = [n for d in details for n in d.get("notices", [])] + [
        i["message"] for d in details for i in d.get("issues", [])
    ]
    if any(r["outcome"] == "duplicate" for r in rows):
        return "not_order", "Duplicate of a message already read"
    return "not_order", notes[0] if notes else "No order or change found"


def order_summary(conn: sqlite3.Connection, provider: str, thread_id: str) -> dict[str, Any] | None:
    """The latest proposal of a conversation in a few words for the board: PO, customer, total, ERP order."""
    found = drafts_svc.drafts_for_thread(conn, provider, thread_id)
    if not found:
        return None
    d = found[-1]
    amounts = [line_amount(ln.unit_price, ln.quantity if ln.unit else None) for ln in d.lines]
    known = [a for a in amounts if a is not None]
    return {
        "po_number": d.po_number,
        "customer_name": d.customer_name,
        "lines": len(d.lines),
        "total_usd": f"{sum(known):.2f}" if known else None,
        "complete": len(known) == len(amounts),
        "erp_order_id": d.erp_order_id,
    }


def draft_view(conn: sqlite3.Connection, d: DraftOut) -> dict[str, Any]:
    out = d.model_dump()
    for ln, raw in zip(d.lines, out["lines"], strict=True):
        amount = line_amount(ln.unit_price, ln.quantity if ln.unit else None)
        raw["amount_usd"] = f"{amount:.2f}" if amount is not None else None
    out["latest_change"] = latest_change(conn, d.draft_id)
    out["revisions"] = [
        {
            "version": r["version"],
            "kind": r["kind"],
            "actor": r["actor"],
            "source_message_id": r["source_message_id"],
            "changes": [describe_change(c) for c in json.loads(r["changes"]) if c.get("field") != "draft"],
            "created_at": r["created_at"],
        }
        for r in db.list_revisions(conn, d.draft_id)
    ]
    return out


def thread_view(
    conn: sqlite3.Connection, thread: SourceThread, atts: dict[tuple[str, str], AttachmentText]
) -> dict[str, Any]:
    processed = {r["message_id"]: r for r in db.list_processed(conn, thread.thread_id)}
    messages = []
    for m in thread.messages:
        suspicious = find_instruction_like(m.body_text)
        attachments = []
        for a in m.attachments:
            at = atts.get((m.message_id, a.attachment_id))
            kind = "image" if a.is_image else "pdf" if a.is_pdf else "other"
            if at and at.status == "ok":
                suspicious += find_instruction_like(at.full_text())
            attachments.append(
                {
                    "attachment_id": a.attachment_id,
                    "filename": a.filename,
                    "size_bytes": a.size_bytes,
                    "kind": kind,
                    "status": at.status if at else ("ignored" if kind != "pdf" else "error"),
                    "detail": at.detail if at else None,
                    "pages": [{"page": p.page, "text": p.text} for p in at.pages] if at else [],
                }
            )
        rec = processed.get(m.message_id)
        messages.append(
            {
                "message_id": m.message_id,
                "sender_email": m.sender_email,
                "sender_name": m.sender_name,
                "subject": m.subject,
                "received_at": m.received_at,
                "body_text": m.body_text,
                "attachments": attachments,
                "instruction_like": suspicious,
                "processed": rec["outcome"] if rec else None,
            }
        )
    found = drafts_svc.drafts_for_thread(conn, thread.provider, thread.thread_id)
    unprocessed = [m.message_id for m in thread.messages if m.message_id not in processed]
    lane, reason = lane_for(conn, thread.provider, thread.thread_id, len(thread.messages))
    held = []
    for r in processed.values():
        if r["outcome"] == "needs_review" and not r["draft_id"]:
            held.append(
                {
                    "message_id": r["message_id"],
                    "issues": [i["message"] for i in json.loads(r["detail"] or "{}").get("issues", [])],
                }
            )
    return {
        "thread_id": thread.thread_id,
        "provider": thread.provider,
        "subject": thread.subject,
        "messages": messages,
        "unprocessed": unprocessed,
        "lane": lane,
        "reason": reason,
        "action": "Prepare proposal" if not found and not processed else "Analyze update",
        "drafts": [draft_view(conn, d) for d in found],
        "held": held,
    }


def run_view(result: ThreadResult, calls: list[dict[str, Any]]) -> dict[str, Any]:
    setup = result.seconds - result.server_start_seconds - sum(o.seconds for o in result.outcomes)
    return {
        "seconds": result.seconds,
        "server_start_seconds": result.server_start_seconds,
        "setup_seconds": setup if result.outcomes and setup >= 0.5 else 0.0,
        "error": result.error,
        "outcomes": [
            {
                "message_id": o.message_id,
                "outcome": o.outcome,
                "extractor": o.extractor,
                "draft_id": o.draft_id,
                "version": o.version,
                "seconds": o.seconds,
                "error": o.error,
                "notices": o.notices,
                "steps": [asdict(s) for s in o.steps],
                "usage": asdict(o.usage) if o.usage else None,
            }
            for o in result.outcomes
        ],
        "calls": calls,
    }


def tool_calls_since(conn: sqlite3.Connection, thread_id: str, event_id: int) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
        "SELECT actor, detail FROM audit_events WHERE event='tool_call' AND thread_id=? AND event_id>? "
        "ORDER BY event_id",
        (thread_id, event_id),
    ).fetchall():
        d = json.loads(r["detail"])
        out.append(
            {
                "actor": r["actor"],
                "server": d.get("server", "-"),
                "tool": d.get("tool", "?"),
                "status": d.get("status", ""),
            }
        )
    return out


def last_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM audit_events").fetchone()[0]
