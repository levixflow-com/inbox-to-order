"""MIME normalization, HTML handling, threading, base64url, PDF extraction limits."""

from __future__ import annotations

import base64
from email.message import EmailMessage

import pytest

from inbox_to_order.services import pdf_extract
from inbox_to_order.services.email_parser import b64url_decode, html_to_text, parse_rfc822, strip_quoted_reply
from inbox_to_order.services.pdf_extract import extract_pdf_text


def test_fixture_threads_follow_reply_headers(fixture_inbox):
    ids = [t.thread_id for t in fixture_inbox.list_threads(None, 25).threads]
    assert ids[0] == "thread-A_initial_order"
    thread = fixture_inbox.get_thread("thread-A_initial_order")
    assert [m.message_id for m in thread.messages] == ["A_initial_order", "B_followup_change"]
    # The resend (E) is a separate conversation, even though its subject is identical.
    assert "thread-E_duplicate_message" in ids


def test_plain_text_body_and_pdf_attachment(fixture_inbox):
    a = fixture_inbox.get_thread("thread-A_initial_order").messages[0]
    assert a.sender_email == "purchasing@pineridge.example.com"
    assert "ship to our Columbus receiving dock" in a.body_text
    assert a.received_at == "2026-11-02T14:14:00+00:00"  # ISO in UTC
    assert [(x.filename, x.mime_type, x.is_pdf, x.is_image) for x in a.attachments] == [
        ("PO-48217.pdf", "application/pdf", True, False),
        ("image001.png", "image/png", False, True),
    ]


@pytest.mark.anyio
async def test_signature_logo_is_ignored_not_read(settings):
    """Images (logos in signatures) are neither read nor sent to the model, and do not block the order."""
    from inbox_to_order.orchestrator import process_thread

    res = await process_thread(settings, "thread-A_initial_order", max_messages=1)
    out = res.outcomes[0]
    step = next(s for s in out.steps if s.name == "Read PDF")
    assert "image001.png: image, ignored" in step.detail and step.status == "done"
    assert out.outcome == "created" and not any("image001" in i.message for i in out.issues)


def test_html_only_reply_is_converted_and_quote_removed(fixture_inbox):
    b = fixture_inbox.get_thread("thread-A_initial_order").messages[1]
    assert b.body_text.startswith("Small change to PO-48217: make the packing tape eight cases, not six.")
    assert "Columbus" not in b.body_text  # quoted earlier message dropped
    assert "<p>" not in b.body_text


def test_html_to_text_drops_scripts_and_styles():
    text = html_to_text("<style>p{}</style><p>Hello&nbsp;<b>there</b></p><script>alert(1)</script><ul><li>a</li></ul>")
    assert "alert" not in text and "p{}" not in text
    assert "Hello" in text and "there" in text and "- a" in text


def test_strip_plain_quoted_reply():
    assert strip_quoted_reply("New text\n> old line\nOn Mon, Nov 2, 2026, Dana wrote:\nold") == "New text"
    # a localized (Italian) reply header with nothing quoted after it
    it = "Make it eight.\n\nvenerdì 6 novembre 2026 17:53, sam <sam@example.net> ha scritto:"
    assert strip_quoted_reply(it) == "Make it eight."
    assert strip_quoted_reply("This is what the buyer wrote:\n8 cases") == "This is what the buyer wrote:\n8 cases"


def test_charset_handling_latin1():
    m = EmailMessage()
    m["From"] = "Café Buyer <buyer@example.org>"
    m["Subject"] = "Résumé of PO 12345"
    m.set_content("Bonjour, quantité: 3 cases", charset="latin-1")
    parsed = parse_rfc822(m.as_bytes(), provider="fixture", message_id="x", thread_id="t")
    assert "quantité" in parsed.message.body_text
    assert parsed.message.subject == "Résumé of PO 12345"


def test_base64url_without_padding():
    raw = b"Subject: hi\r\n\r\nbody??>>"
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert b64url_decode(encoded) == raw


def test_duplicate_resend_has_same_content_hash(fixture_inbox):
    a = fixture_inbox.get_thread("thread-A_initial_order").messages[0]
    e = fixture_inbox.get_thread("thread-E_duplicate_message").messages[0]
    assert a.message_id != e.message_id and a.content_hash == e.content_hash
    b = fixture_inbox.get_thread("thread-A_initial_order").messages[1]
    assert b.content_hash != a.content_hash


def test_pdf_text_extraction_keeps_pages(fixture_inbox):
    att = fixture_inbox.get_attachment("A_initial_order", "att-1")
    res = extract_pdf_text(
        att.data, message_id="A", attachment_id="att-1", filename="PO-48217.pdf", mime_type="application/pdf"
    )
    assert res.status == "ok" and res.pages[0].page == 1
    assert "1 | PR-TAPE-2 | Packing tape, 2 in x 55 yd | 6 | case" in res.full_text()


def test_image_only_pdf_is_reported_not_read(fixture_inbox):
    att = fixture_inbox.get_attachment("F_image_only_pdf", "att-1")
    res = extract_pdf_text(
        att.data, message_id="F", attachment_id="att-1", filename="scan.pdf", mime_type="application/pdf"
    )
    assert res.status == "no_text" and res.pages == []
    assert "OCR is outside the scope of this proof of concept" in (res.detail or "")


def test_unsupported_and_broken_attachments():
    assert (
        extract_pdf_text(
            b"PK\x03\x04zip", message_id="m", attachment_id="a", filename="x.zip", mime_type="application/zip"
        ).status
        == "not_supported"
    )
    assert extract_pdf_text(
        b"%PDF-1.4 garbage", message_id="m", attachment_id="a", filename="x.pdf", mime_type="application/pdf"
    ).status in ("error", "no_text")


def test_oversize_pdf_rejected(monkeypatch, fixture_inbox):
    monkeypatch.setattr(pdf_extract, "MAX_ATTACHMENT_BYTES", 100)
    att = fixture_inbox.get_attachment("A_initial_order", "att-1")
    res = pdf_extract.extract_pdf_text(
        att.data, message_id="A", attachment_id="att-1", filename="a.pdf", mime_type="application/pdf"
    )
    assert res.status == "too_large" and res.pages == []
