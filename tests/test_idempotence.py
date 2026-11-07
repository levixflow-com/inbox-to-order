"""Demo E: reprocessing never creates a second proposal, line, revision or ERP order."""

from __future__ import annotations

import pytest

from inbox_to_order.evaluation import evaluate_fixtures
from inbox_to_order.models import DraftLineIn, Evidence, Provenance, SaveDraftRequest, ShipToIn
from inbox_to_order.orchestrator import process_thread
from inbox_to_order.services import drafts

pytestmark = pytest.mark.anyio


def counts(conn) -> tuple[int, int, int, int]:
    return tuple(
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM order_drafts), (SELECT COUNT(*) FROM draft_lines), "
            "(SELECT COUNT(*) FROM draft_revisions), (SELECT COUNT(*) FROM processed_sources)"
        ).fetchone()
    )


async def test_replaying_a_thread_changes_nothing(settings, conn):
    await process_thread(settings, "thread-A_initial_order")
    first = counts(conn)
    assert first == (1, 4, 2, 2)
    replay = await process_thread(settings, "thread-A_initial_order")
    assert [o.outcome for o in replay.outcomes] == ["already_processed", "already_processed"]
    assert counts(conn) == first


async def test_resent_copy_is_recognized_as_duplicate(settings, conn):
    await process_thread(settings, "thread-A_initial_order", max_messages=1)
    res = await process_thread(settings, "thread-E_duplicate_message")
    out = res.outcomes[0]
    assert out.outcome == "duplicate" and out.draft_id == 1
    assert counts(conn) == (1, 4, 1, 2)


def test_save_is_idempotent_per_source_message(conn):
    ev = Evidence(message_id="m1", location="body", excerpt="abc")
    req = SaveDraftRequest(
        mode="create",
        customer_id="PINE-RIDGE",
        customer_name="Pine Ridge",
        po_number="PO-777",
        ship_to=ShipToIn(address_id="PR-DAYTON", status="matched"),
        lines=[
            DraftLineIn(
                customer_item_code="PR-WRAP-12", sku="HS-3301", quantity=2, unit="roll", status="matched", evidence=[ev]
            )
        ],
        provenance=Provenance(provider="fixture", message_id="m1", thread_id="t", content_hash="h", extractor="test"),
    )
    assert drafts.save_draft(conn, req).outcome == "created"
    again = drafts.save_draft(conn, req)
    assert again.ok and again.outcome == "replay" and again.draft_id == 1
    assert counts(conn) == (1, 1, 1, 1)


async def test_all_fixture_expectations_with_rule_based_extractor(settings):
    """All documented fixtures through the real MCP servers, plus a full replay and an untouched Demo ERP."""
    report = await evaluate_fixtures(settings)
    assert report.extractor == "rules"
    assert report.passed, report.failures
    assert report.checks >= 55
