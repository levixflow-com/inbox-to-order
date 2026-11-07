"""Demo B, demo H and their edge cases: a change email updates the existing order as a new revision,
or asks a person. The Demo ERP is never written by processing alone."""

from __future__ import annotations

import json

import pytest

from inbox_to_order.extractors import RuleBasedExtractor
from inbox_to_order.models import Provenance, SaveDraftRequest, SourceMessage
from inbox_to_order.orchestrator import process_thread
from inbox_to_order.pipeline import MessageContext, build_plan
from inbox_to_order.review import confirm_and_write
from inbox_to_order.services import drafts, erp

from .conftest import InProcessToolbox, attachment_texts

pytestmark = pytest.mark.anyio
THREAD = "thread-A_initial_order"


def _ctx(conn, erp_conn, inbox, msg: SourceMessage, thread_id: str = THREAD, thread_drafts=None) -> MessageContext:
    return MessageContext(
        thread=inbox.get_thread(thread_id),
        message=msg,
        attachments=attachment_texts(inbox, msg.message_id) if msg.attachments else [],
        customer=erp.find_customer(erp_conn, msg.sender_email),
        thread_drafts=thread_drafts
        if thread_drafts is not None
        else drafts.drafts_for_thread(conn, "fixture", thread_id),
        find_local_draft=lambda cid, po: drafts.find_draft(conn, cid, po),
    )


async def _apply(conn, erp_conn, inbox, msg: SourceMessage, thread_drafts=None, thread_id: str = THREAD):
    box = InProcessToolbox(erp_conn)
    ctx = _ctx(conn, erp_conn, inbox, msg, thread_id, thread_drafts)
    extraction = await RuleBasedExtractor().extract(ctx, box)  # type: ignore[arg-type]
    prov = Provenance(
        provider="fixture",
        message_id=msg.message_id,
        thread_id=thread_id,
        content_hash=msg.content_hash,
        extractor="rules",
    )
    plan = await build_plan(ctx, extraction, box, prov)  # type: ignore[arg-type]
    saved = drafts.save_draft(conn, plan.request) if plan.request else None
    return plan, saved


def _followup(
    body: str,
    mid: str = "X1",
    subject: str = "Re: PO-48217 - next week's delivery",
    sender: str = "purchasing@pineridge.example.com",
    thread_id: str = THREAD,
) -> SourceMessage:
    return SourceMessage(
        provider="fixture",
        message_id=mid,
        thread_id=thread_id,
        sender_email=sender,
        subject=subject,
        received_at="2026-11-03T10:00:00+00:00",
        body_text=body,
        content_hash=f"hash-{mid}",
    )


@pytest.fixture
async def draft_v1(conn, erp_conn, fixture_inbox):
    initial = fixture_inbox.get_thread(THREAD).messages[0]
    _, saved = await _apply(conn, erp_conn, fixture_inbox, initial)
    assert saved and saved.outcome == "created"
    return saved


async def test_followup_updates_existing_proposal_end_to_end(settings, conn):
    """Through the real MCP servers: 6 -> 8 cases on line 1, nothing else changes, no second order."""
    first = await process_thread(settings, THREAD, max_messages=1)
    assert first.outcomes[0].outcome == "created"
    before = drafts.get_draft_by_id(conn, first.outcomes[0].draft_id)
    second = await process_thread(settings, THREAD, max_messages=1)
    out = second.outcomes[0]
    assert (out.message_id, out.outcome, out.version) == ("B_followup_change", "amended", 2)
    assert [(c.line_no, c.before, c.after) for c in out.changes] == [(1, "6 case", "8 case")]
    after = drafts.get_draft_by_id(conn, out.draft_id)
    assert after.lines[0].quantity == 8 and after.lines[0].evidence[-1].message_id == "B_followup_change"
    assert [ln.model_dump() for ln in after.lines[1:]] == [ln.model_dump() for ln in before.lines[1:]]
    assert after.ship_to == before.ship_to
    assert conn.execute("SELECT COUNT(*) FROM order_drafts").fetchone()[0] == 1
    revs = conn.execute("SELECT * FROM draft_revisions ORDER BY version").fetchall()
    assert [(r["version"], r["kind"], r["actor"], r["source_message_id"]) for r in revs] == [
        (1, "created", "assistant", "A_initial_order"),
        (2, "amended", "assistant", "B_followup_change"),
    ]
    assert json.loads(revs[1]["changes"])[0] == {
        "field": "quantity",
        "line_no": 1,
        "before": "6 case",
        "after": "8 case",
    }


async def test_change_to_an_order_that_exists_only_in_the_erp(settings, conn, erp_conn):
    """Demo H: the email changes PO-48190, which this app never saw. The proposal starts as a copy of the
    ERP order (v1) and the change is v2. The ERP itself is untouched until an operator confirms."""
    res = await process_thread(settings, "thread-H_erp_order_change")
    out = res.outcomes[0]
    assert (out.outcome, out.version) == ("amended", 2), out
    assert [(c.line_no, c.before, c.after) for c in out.changes] == [(2, "10 roll", "12 roll")]
    d = drafts.get_draft_by_id(conn, out.draft_id)
    assert (d.status, d.erp_order_id, d.erp_version) == ("ready", "SO-0950", 1)
    revs = conn.execute(
        "SELECT kind, actor FROM draft_revisions WHERE draft_id=? ORDER BY version", (d.draft_id,)
    ).fetchall()
    assert [tuple(r) for r in revs] == [("copied_from_erp", "demo-erp"), ("amended", "assistant")]
    order = erp.get_order_by_id(erp_conn, "SO-0950")
    assert order.version == 1 and order.lines[1].quantity == 10  # not written yet


async def test_amending_a_confirmed_proposal_requires_a_new_confirmation(conn, erp_conn, fixture_inbox, draft_v1):
    box = InProcessToolbox(erp_conn)
    written = await confirm_and_write(conn, box, draft_v1.draft_id, 1)  # type: ignore[arg-type]
    assert written.outcome == "created"
    plan, saved = await _apply(conn, erp_conn, fixture_inbox, fixture_inbox.get_thread(THREAD).messages[1])
    d = drafts.get_draft_by_id(conn, saved.draft_id)
    assert (
        d.status == "ready" and d.version == 2 and d.confirmed_version == 1 and d.erp_order_id == written.order.order_id
    )
    assert any("needs a new confirmation" in n for n in plan.notices)
    assert erp.get_order_by_id(erp_conn, written.order.order_id).lines[0].quantity == 6  # ERP unchanged until confirmed


async def test_new_order_email_for_a_po_already_in_the_erp_is_not_created_twice(conn, erp_conn, fixture_inbox):
    msg = _followup(
        "Please book PO-48190 for 3 cases of packing tape.",
        mid="H2",
        subject="PO-48190",
        sender="buying@lakeshore.example.org",
        thread_id="thread-H_erp_order_change",
    )
    plan, saved = await _apply(
        conn, erp_conn, fixture_inbox, msg, thread_drafts=[], thread_id="thread-H_erp_order_change"
    )
    assert saved is None and any("already exists in the Demo ERP as order SO-0950" in i.message for i in plan.issues)


async def test_stated_previous_quantity_must_match(conn, erp_conn, fixture_inbox, draft_v1):
    await _apply(
        conn,
        erp_conn,
        fixture_inbox,
        _followup("Small change to PO-48217: make the packing tape eight cases, not five."),
    )
    d = drafts.get_draft_by_id(conn, draft_v1.draft_id)
    assert d.lines[0].quantity == 6 and d.status == "needs_review"
    assert any("was 5" in i.message for i in d.issues)


async def test_unit_change_is_never_converted(conn, erp_conn, fixture_inbox, draft_v1):
    await _apply(conn, erp_conn, fixture_inbox, _followup("PO-48217: make the packing tape eight rolls, not six."))
    d = drafts.get_draft_by_id(conn, draft_v1.draft_id)
    assert (d.lines[0].quantity, d.lines[0].unit) == (6, "case")
    assert any("No conversion" in i.message for i in d.issues)


async def test_ambiguous_target_asks_instead_of_guessing(conn, erp_conn, fixture_inbox, draft_v1):
    await _apply(conn, erp_conn, fixture_inbox, _followup("PO-48217: please change the blue labels to twelve."))
    d = drafts.get_draft_by_id(conn, draft_v1.draft_id)
    assert [ln.quantity for ln in d.lines] == [6, 10, 4, 2]
    assert d.status == "needs_review" and any("Which line" in i.message for i in d.issues)


async def test_from_to_phrasing(conn, erp_conn, fixture_inbox, draft_v1):
    _, saved = await _apply(
        conn, erp_conn, fixture_inbox, _followup("PO-48217: please change the bubble wrap from 4 rolls to 7 rolls.")
    )
    assert saved.outcome == "amended" and [(c.line_no, c.before, c.after) for c in saved.changes] == [
        (3, "4 roll", "7 roll")
    ]


async def test_po_conflicting_with_thread_requires_manual_resolution(conn, erp_conn, fixture_inbox, draft_v1):
    msg = _followup("PO-99999: make the packing tape eight cases, not six.", subject="Re: quick change")
    plan, saved = await _apply(conn, erp_conn, fixture_inbox, msg)
    assert saved is None and plan.request is None
    assert any("linked to PO-48217" in i.message for i in plan.issues)


async def test_amendment_without_existing_order(conn, erp_conn, fixture_inbox):
    plan, saved = await _apply(
        conn,
        erp_conn,
        fixture_inbox,
        _followup("PO-48217: make the packing tape eight cases, not six."),
        thread_drafts=[],
    )
    assert saved is None and any("no such order exists" in i.message for i in plan.issues)


async def test_stale_version_is_rejected(conn, erp_conn, fixture_inbox, draft_v1):
    msg = fixture_inbox.get_thread(THREAD).messages[1]
    box = InProcessToolbox(erp_conn)
    ctx = _ctx(conn, erp_conn, fixture_inbox, msg)
    ex = await RuleBasedExtractor().extract(ctx, box)  # type: ignore[arg-type]
    prov = Provenance(
        provider="fixture",
        message_id=msg.message_id,
        thread_id=THREAD,
        content_hash=msg.content_hash,
        extractor="rules",
    )
    plan = await build_plan(ctx, ex, box, prov)  # type: ignore[arg-type]
    # Someone else saves a revision first.
    await _apply(conn, erp_conn, fixture_inbox, _followup("PO-48217: change line 3 to 5 rolls.", mid="X9"))
    res = drafts.save_draft(conn, SaveDraftRequest.model_validate(plan.request.model_dump()))
    assert not res.ok and res.error_code == "VERSION_CONFLICT"
    assert drafts.get_draft_by_id(conn, draft_v1.draft_id).lines[0].quantity == 6


async def test_two_different_po_numbers_in_one_message_require_review(conn, erp_conn, fixture_inbox, draft_v1):
    plan, saved = await _apply(
        conn, erp_conn, fixture_inbox, _followup("PO-99999: make the packing tape eight cases, not six.")
    )
    assert saved is None and any("several PO numbers" in i.message for i in plan.issues)
