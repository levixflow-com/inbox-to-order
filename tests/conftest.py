"""Shared fixtures. No test here needs Gmail credentials or an AI API key."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inbox_to_order import db
from inbox_to_order.config import FIXTURES_DIR, Settings, load_settings
from inbox_to_order.models import AttachmentText, CreateOrderRequest, UpdateOrderRequest
from inbox_to_order.services import erp
from inbox_to_order.services.inbox_sources import FixtureInbox
from inbox_to_order.services.pdf_extract import extract_pdf_text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fresh app database and Demo ERP per test; env vars are inherited by the MCP server subprocesses."""
    db_path = tmp_path / "test.db"
    erp_path = tmp_path / "erp.db"
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "EXTRA_CUSTOMER_SENDERS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INBOX_MODE", "fixture")
    monkeypatch.setenv("EXTRACTOR", "rules")
    monkeypatch.setenv("INBOX_TO_ORDER_DB", str(db_path))
    monkeypatch.setenv("DEMO_ERP_DB", str(erp_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(tmp_path / "missing-credentials.json"))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "missing-token.json"))
    s = replace(load_settings(), db_path=db_path, erp_db_path=erp_path, fixtures_dir=FIXTURES_DIR)
    db.init_db(s, reset=True)
    return s


@pytest.fixture
def conn(settings: Settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def erp_conn(settings: Settings):
    c = erp.open_erp(settings.erp_db_path)
    yield c
    c.close()


@pytest.fixture(scope="session")
def fixture_inbox() -> FixtureInbox:
    return FixtureInbox(FIXTURES_DIR / "messages")


def attachment_texts(inbox: FixtureInbox, message_id: str) -> list[AttachmentText]:
    thread_msgs = {m.message_id: m for t in inbox._threads for m in inbox.get_thread(t).messages}
    out = []
    for meta in (a for a in thread_msgs[message_id].attachments if a.is_pdf):  # like the orchestrator: PDFs only
        raw = inbox.get_attachment(message_id, meta.attachment_id)
        out.append(
            extract_pdf_text(
                raw.data,
                message_id=message_id,
                attachment_id=meta.attachment_id,
                filename=meta.filename,
                mime_type=meta.mime_type,
            )
        )
    return out


class InProcessToolbox:
    """Same call interface as mcp_client.Toolbox for the Demo ERP tools, but in-process, for fast
    pipeline unit tests. The real protocol boundary is covered in test_mcp_integration.py and the
    end-to-end tests.
    """

    def __init__(self, erp_conn: sqlite3.Connection) -> None:
        self.conn = erp_conn
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_call = None

    async def call(self, name: str, args: dict[str, Any], *, actor: str = "app") -> dict[str, Any]:
        self.calls.append((name, args))
        c = self.conn
        handlers = {
            "find_customer": lambda: erp.find_customer(c, args["sender_or_name"]),
            "get_customer": lambda: erp.get_customer(c, args["customer_id"]),
            "find_product": lambda: erp.find_product(c, args["customer_id"], args["customer_code_or_description"]),
            "list_products": lambda: erp.list_products(c, args["customer_id"]),
            "get_order": lambda: erp.get_order(c, args["customer_id"], args["po_number"]),
            "list_orders": lambda: erp.list_orders(c, args.get("limit", 20)),
            "create_order": lambda: erp.create_order(c, CreateOrderRequest.model_validate(args["request"])),
            "update_order": lambda: erp.update_order(c, UpdateOrderRequest.model_validate(args["request"])),
        }
        if name not in handlers:
            raise AssertionError(f"unexpected tool {name}")
        try:
            return handlers[name]().model_dump(mode="json")
        except erp.ErpError as exc:
            from inbox_to_order.mcp_client import ToolCallError

            raise ToolCallError(name, exc.code, exc.message) from exc

    def record(self, *a: Any) -> None:  # noqa: D401
        pass
