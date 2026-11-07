"""Tools are discovered and invoked across a real MCP stdio transport (two subprocess servers)."""

from __future__ import annotations

import pytest

from inbox_to_order.mcp_client import (
    MODEL_ALLOWED_TOOLS,
    SERVER_MODULES,
    ToolCallError,
    _server_params,
    anthropic_tool_definitions,
    open_toolbox,
)

pytestmark = pytest.mark.anyio

EXPECTED = {
    "mail": {"list_order_threads", "get_order_thread", "get_text_attachment"},
    "erp": {
        "find_customer",
        "get_customer",
        "find_product",
        "list_products",
        "get_order",
        "list_orders",
        "create_order",
        "update_order",
    },
}
ERP_WRITES = {"create_order", "update_order"}
MAIL_FORBIDDEN_WORDS = ("send", "delete", "trash", "modify", "label", "forward", "reply", "draft")


async def test_discovery_over_stdio(settings):
    async with open_toolbox() as box:
        by_server: dict[str, set[str]] = {}
        for name, (server, tool) in box.tools.items():
            by_server.setdefault(server, set()).add(name)
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema is not None, f"{name} must publish a structured output schema"
        assert by_server == EXPECTED
        for name in EXPECTED["mail"]:
            assert not any(w in name for w in MAIL_FORBIDDEN_WORDS), name
        for name in (EXPECTED["mail"] | EXPECTED["erp"]) - ERP_WRITES:
            assert box.tools[name][1].annotations.read_only_hint is True, name
        for name in ERP_WRITES:
            ann = box.tools[name][1].annotations
            assert ann.read_only_hint is False and ann.destructive_hint is False and ann.idempotent_hint is True
        for client in box.clients.values():
            assert client.protocol_version  # negotiated over the transport


async def test_invocation_returns_structured_content_and_explicit_errors(settings):
    seen = []
    async with open_toolbox(lambda actor, server, tool, args, status: seen.append((server, tool, status))) as box:
        listing = await box.call("list_order_threads", {"limit": 3})
        assert listing["provider"] == "fixture" and len(listing["threads"]) == 3
        att = await box.call("get_text_attachment", {"message_id": "A_initial_order", "attachment_id": "att-1"})
        assert att["status"] == "ok" and att["pages"][0]["page"] == 1
        with pytest.raises(ToolCallError) as exc:
            await box.call("get_order_thread", {"thread_id": "thread-does-not-exist"})
        assert exc.value.code == "THREAD_NOT_FOUND"
        with pytest.raises(ToolCallError) as exc:
            await box.call("send_email", {"to": "attacker@example.com"})
        assert exc.value.code == "UNKNOWN_TOOL"
    assert ("mail", "list_order_threads", "ok") in seen
    assert ("mail", "get_order_thread", "error:THREAD_NOT_FOUND") in seen
    assert ("-", "send_email", "refused_unknown_tool") in seen


async def test_model_sees_only_allowlisted_tools(settings):
    async with open_toolbox() as box:
        names = {d["name"] for d in anthropic_tool_definitions(box)}
    assert names == set(MODEL_ALLOWED_TOOLS)
    assert not names & ERP_WRITES and "list_order_threads" not in names and "list_orders" not in names


def test_servers_do_not_receive_the_ai_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    for module in SERVER_MODULES.values():
        params = _server_params(module)
        assert "ANTHROPIC_API_KEY" not in (params.env or {})
        assert params.args == ["-m", module]


async def test_erp_write_and_its_errors_cross_the_protocol(settings):
    request = {
        "customer_id": "PINE-RIDGE",
        "po_number": "PO-6001",
        "ship_to_address_id": "PR-DAYTON",
        "lines": [{"sku": "HS-3301", "quantity": 2, "unit": "roll"}],
        "idempotency_key": "mcp-test-0001",
    }
    async with open_toolbox() as box:
        created = await box.call("create_order", {"request": request})
        assert created["outcome"] == "created" and created["order"]["version"] == 1
        replay = await box.call("create_order", {"request": request})
        assert replay["outcome"] == "replay" and replay["order"]["order_id"] == created["order"]["order_id"]
        with pytest.raises(ToolCallError) as exc:
            await box.call(
                "update_order",
                {
                    "request": {
                        "order_id": created["order"]["order_id"],
                        "expected_version": 7,
                        "ship_to_address_id": "PR-DAYTON",
                        "lines": [{"sku": "HS-3301", "quantity": 3, "unit": "roll"}],
                        "idempotency_key": "mcp-test-0002",
                    }
                },
            )
        assert exc.value.code == "VERSION_CONFLICT"
        found = await box.call("get_order", {"customer_id": "PINE-RIDGE", "po_number": "PO-6001"})
        assert found["found"] and found["order"]["lines"][0]["quantity"] == 2
