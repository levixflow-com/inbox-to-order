"""Gmail connector against MOCKED Gmail API responses (AT-08, AT-12).

These tests do not contact Google. They verify decoding, normalization, error mapping and that only
read methods are used. Real OAuth against a live mailbox is a separate, manual step (docs/GMAIL_SETUP.md).
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from inbox_to_order.config import FIXTURES_DIR
from inbox_to_order.mcp_client import ToolCallError, open_toolbox
from inbox_to_order.orchestrator import process_thread
from inbox_to_order.services import gmail_auth
from inbox_to_order.services.gmail_auth import GmailAuthError, GmailNotConfigured, load_credentials
from inbox_to_order.services.inbox_sources import FixtureInbox, GmailInbox, InboxError

MSG_DIR = FIXTURES_DIR / "messages"


class _Req:
    def __init__(self, svc: FakeGmail, method: str, kwargs: dict) -> None:
        self.svc, self.method, self.kwargs = svc, method, kwargs

    def execute(self, num_retries: int = 0):  # noqa: ARG002
        self.svc.calls.append(self.method)
        if self.svc.fail is not None:
            raise self.svc.fail
        return self.svc.handle(self.method, self.kwargs)


class FakeGmail:
    """Mimics service.users().threads()/messages() with two messages from the fixtures."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: Exception | None = None
        raw_a = (MSG_DIR / "A_initial_order.eml").read_bytes()
        raw_b = (MSG_DIR / "B_followup_change.eml").read_bytes()
        self._msgs = {
            "18f0a1": {
                "id": "18f0a1",
                "threadId": "18f0a0",
                "internalDate": "1793632440000",
                "raw": base64.urlsafe_b64encode(raw_a).decode().rstrip("="),
            },
            "18f0b2": {
                "id": "18f0b2",
                "threadId": "18f0a0",
                "internalDate": "1793645700000",
                "raw": base64.urlsafe_b64encode(raw_b).decode().rstrip("="),
            },
        }

    def users(self):
        return self

    def threads(self):
        return _Namespace(self, "threads")

    def messages(self):  # noqa: F811 - mimics the client API shape
        return _Namespace(self, "messages")

    def handle(self, method: str, kw: dict):
        if method == "threads.list":
            return {"threads": [{"id": "18f0a0"}]}
        if method == "threads.get":
            if kw["id"] != "18f0a0":
                raise HttpError(httplib2.Response({"status": 404}), b"not found")
            # Like the real API: format="metadata" returns headers only, never the MIME parts.
            with_parts = kw.get("format") == "full"
            msgs = [
                {
                    "id": m,
                    "internalDate": v["internalDate"],
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "PO-48217 - next week's delivery"},
                            {"name": "From", "value": "Dana <purchasing@pineridge.example.com>"},
                        ],
                        **({"parts": [{"filename": "PO-48217.pdf"}] if m == "18f0a1" else []} if with_parts else {}),
                    },
                }
                for m, v in self.messages_data().items()
            ]
            return {"id": "18f0a0", "messages": msgs}
        if method == "messages.get":
            assert kw["format"] == "raw"
            return self.messages_data()[kw["id"]]
        raise AssertionError(method)

    def messages_data(self) -> dict:
        return self._msgs


class _Namespace:
    def __init__(self, svc: FakeGmail, kind: str) -> None:
        self.svc, self.kind = svc, kind

    def list(self, **kw):
        return _Req(self.svc, f"{self.kind}.list", kw)

    def get(self, **kw):
        return _Req(self.svc, f"{self.kind}.get", kw)


@pytest.fixture
def gmail(settings) -> tuple[GmailInbox, FakeGmail]:
    fake = FakeGmail()
    return GmailInbox(replace(settings, inbox_mode="gmail"), service=fake), fake


def test_gmail_messages_normalize_exactly_like_fixtures(gmail):
    inbox, fake = gmail
    listing = inbox.list_threads(None, 10)
    assert listing.provider == "gmail" and listing.threads[0].message_count == 2
    assert listing.threads[0].attachment_count == 1
    thread = inbox.get_thread("18f0a0")
    fx = FixtureInbox(MSG_DIR).get_thread("thread-A_initial_order")
    for g, f in zip(thread.messages, fx.messages, strict=True):
        assert (g.body_text, g.subject, g.sender_email, g.content_hash) == (
            f.body_text,
            f.subject,
            f.sender_email,
            f.content_hash,
        )
        assert g.provider == "gmail" and g.thread_id == "18f0a0"
    pdf = inbox.get_attachment("18f0a1", "att-1")
    assert pdf.data.startswith(b"%PDF") and pdf.meta.filename == "PO-48217.pdf"
    assert set(fake.calls) <= {"threads.list", "threads.get", "messages.get"}  # read-only methods only


def test_gmail_errors_are_mapped(gmail):
    inbox, fake = gmail
    with pytest.raises(InboxError) as exc:
        inbox.get_thread("missing")
    assert exc.value.code == "NOT_FOUND"
    fake.fail = HttpError(httplib2.Response({"status": 401}), b"unauthorized")
    with pytest.raises(GmailAuthError, match="gmail-auth"):
        inbox.list_threads(None, 5)
    fake.fail = RefreshError("invalid_grant: Token has been expired or revoked.")
    with pytest.raises(GmailAuthError, match="expired or was revoked"):
        inbox.list_threads(None, 5)


def test_gmail_class_has_no_write_methods():
    public = {n for n in dir(GmailInbox) if not n.startswith("_")}
    assert public == {"get_attachment", "get_thread", "list_threads", "provider", "service"}


def test_not_configured_messages(settings):
    with pytest.raises(GmailNotConfigured, match="docs/GMAIL_SETUP.md"):
        load_credentials(settings)
    settings.gmail_credentials_file.write_text("{}")
    with pytest.raises(GmailNotConfigured, match="gmail-auth"):
        load_credentials(settings)


def _write_token(path: Path, scopes: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "token": "expired-access-token",
                "refresh_token": "refresh-token",
                "client_id": "cid.apps.googleusercontent.com",
                "client_secret": "not-a-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": scopes,
                "expiry": "2020-01-01T00:00:00Z",
            }
        )
    )


def test_expired_or_revoked_token_asks_for_reauthorization(settings, monkeypatch):
    """AT-12: refresh failure surfaces a clear message; nothing falls back to fake data."""
    settings.gmail_credentials_file.write_text("{}")
    _write_token(settings.gmail_token_file, gmail_auth.GMAIL_SCOPES)

    def boom(self, request):  # noqa: ANN001, ARG001
        raise RefreshError("invalid_grant")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)
    with pytest.raises(GmailAuthError) as exc:
        load_credentials(settings)
    assert "7 days" in str(exc.value) and "gmail-auth" in str(exc.value)


def test_token_with_wrong_scope_is_rejected(settings):
    settings.gmail_credentials_file.write_text("{}")
    _write_token(settings.gmail_token_file, ["https://www.googleapis.com/auth/gmail.metadata"])
    with pytest.raises(GmailAuthError, match="gmail.readonly"):
        load_credentials(settings)


@pytest.mark.anyio
async def test_gmail_not_configured_through_mcp_and_fixture_mode_still_works(settings, conn, monkeypatch):
    """AT-08: Gmail mode without credentials fails clearly over MCP; fixture mode is unaffected."""
    monkeypatch.setenv("INBOX_MODE", "gmail")
    async with open_toolbox() as box:
        with pytest.raises(ToolCallError) as exc:
            await box.call("list_order_threads", {"limit": 5})
    assert exc.value.code == "GMAIL_NOT_CONFIGURED" and "Fixture mode still works" in exc.value.message
    res = await process_thread(replace(settings, inbox_mode="gmail"), "18f0a0")
    assert res.error and res.error.startswith("GMAIL_NOT_CONFIGURED") and not res.outcomes
    assert conn.execute("SELECT COUNT(*) FROM order_drafts").fetchone()[0] == 0  # no fallback posing as live data
    monkeypatch.setenv("INBOX_MODE", "fixture")
    ok = await process_thread(settings, "thread-A_initial_order", max_messages=1)
    assert ok.outcomes[0].outcome == "created"
