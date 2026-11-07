"""Model failures are recoverable and never leave partial data behind.

Also checks that a well-formed (scripted) Claude extraction flows through the same pipeline.
These tests use a scripted fake client; they say nothing about how the real model behaves.
"""

from __future__ import annotations

import httpx2
import pytest
from anthropic import APIConnectionError

from inbox_to_order.agent import ClaudeExtractor
from inbox_to_order.orchestrator import process_thread
from inbox_to_order.services import drafts

from .fake_claude import FakeAnthropic, response, tool_use

pytestmark = pytest.mark.anyio
A = "thread-A_initial_order"


def nothing_persisted(conn) -> bool:
    return (
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM order_drafts) + (SELECT COUNT(*) FROM processed_sources)"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "script",
    [
        [APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))],
        [response(stop_reason="refusal")],
        [response(stop_reason="max_tokens")],
        [response(stop_reason="end_turn")],  # finished without submitting
        [
            response(tool_use("submit_extraction", {"intent": "buy"}, "a")),
            response(tool_use("submit_extraction", {"lines": "nope"}, "b")),
        ],  # invalid twice
        [response(tool_use("get_order_thread", {"thread_id": A}, f"r{i}")) for i in range(8)],  # never submits
    ],
    ids=["network", "refusal", "max_tokens", "no_submit", "invalid_twice", "round_limit"],
)
async def test_failures_persist_nothing_and_can_be_retried(settings, conn, script):
    res = await process_thread(settings, A, extractor=ClaudeExtractor(settings, client=FakeAnthropic(script)))
    first, second = res.outcomes
    assert first.outcome == "error" and first.error
    assert second.outcome == "skipped"  # the follow-up waits for the initial order
    assert nothing_persisted(conn)
    retry = await process_thread(settings, A)  # e.g. after fixing the key, or with the baseline
    assert [o.outcome for o in retry.outcomes] == ["created", "amended"]


async def test_invalid_submission_gets_one_correction_round(settings, conn):
    good = {
        "intent": "new_order",
        "po_number": "PO-48217",
        "po_evidence": {"message_id": "A_initial_order", "location": "subject", "excerpt": "PO-48217"},
        "ship_to_text": "our Columbus receiving dock",
        "ship_to_evidence": {
            "message_id": "A_initial_order",
            "location": "body",
            "excerpt": "Please ship to our Columbus receiving dock",
        },
        "lines": [
            {
                "customer_item_code": "PR-TAPE-2",
                "description": "Packing tape, 2 in x 55 yd",
                "quantity_text": "6 case",
                "evidence": {
                    "message_id": "A_initial_order",
                    "location": "attachment",
                    "attachment_id": "att-1",
                    "page": 1,
                    "excerpt": "1 | PR-TAPE-2 | Packing tape, 2 in x 55 yd | 6 | case",
                },
            }
        ],
    }
    fake = FakeAnthropic(
        [
            response(
                tool_use("get_order_thread", {"thread_id": A}, "g1"),
                tool_use("get_text_attachment", {"message_id": "A_initial_order", "attachment_id": "att-1"}, "g2"),
            ),
            response(tool_use("submit_extraction", {"intent": "new_order", "lines": [{"quantity_text": "6"}]}, "bad")),
            response(tool_use("submit_extraction", good, "ok")),
        ]
    )
    res = await process_thread(settings, A, extractor=ClaudeExtractor(settings, client=fake), max_messages=1)
    out = res.outcomes[0]
    assert out.outcome == "created" and out.extractor == "claude"
    correction = fake.messages.requests[2]["messages"][-1]["content"][0]
    assert correction["is_error"] and "INVALID_EXTRACTION" in correction["content"]
    d = drafts.get_draft_by_id(conn, out.draft_id)
    assert (d.lines[0].sku, d.lines[0].quantity, d.ship_to.address_id) == ("HS-1102", 6, "PR-COLUMBUS")
    # The extractor never sent anything the model could use to skip validation.
    req = fake.messages.requests[0]
    assert req["output_config"] == {"effort": settings.anthropic_effort} and req["model"] == settings.anthropic_model
    # Usage is measured from the API responses and priced at list price (3 calls x 100 in / 20 out in the script).
    assert (out.usage.calls, out.usage.input_tokens, out.usage.output_tokens) == (3, 300, 60)
    assert out.usage.cost_usd is not None and out.usage.cost_usd > 0
    assert all(s.seconds >= 0 for s in out.steps) and out.seconds > 0


@pytest.mark.parametrize(("model", "fallback"), [("claude-haiku-5-5", False), ("claude-sonnet-5-5", True)])
async def test_server_side_fallback_only_where_supported(settings, model, fallback):
    from dataclasses import replace

    s = replace(settings, anthropic_model=model)
    fake = FakeAnthropic([response(stop_reason="end_turn")])
    await process_thread(s, A, extractor=ClaudeExtractor(s, client=fake), max_messages=1)
    req = fake.messages.requests[0]
    assert req["model"] == model
    assert ("fallbacks" in req) is fallback and ("betas" in req) is fallback
