"""Deterministic MIME normalization shared by fixture (.eml) and Gmail (format=raw) sources.

Both providers hand us RFC 822 bytes, so there is exactly one parser and the downstream pipeline
cannot tell (or care) which provider the message came from.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from ..config import MAX_BODY_CHARS
from ..models import AttachmentMeta, Provider, SourceMessage


@dataclass
class RawAttachment:
    meta: AttachmentMeta
    data: bytes


@dataclass
class ParsedEmail:
    message: SourceMessage
    attachments: list[RawAttachment]


def b64url_decode(data: str) -> bytes:
    """Gmail returns base64url without padding; tolerate both padded and unpadded input."""
    data = data.strip().replace("\n", "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _HTMLText(HTMLParser):
    """Small HTML-to-text converter: keeps paragraph breaks, drops scripts/styles and quoted replies."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}
    _SKIP = {"script", "style", "head", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK and not self._skip_depth:
            self.parts.append("\n")
        if tag == "li" and not self._skip_depth:
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK and not self._skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_QUOTE_HEADER = re.compile(r"^\s*On .{3,200} wrote:\s*$", re.IGNORECASE)
# Localized mail clients ("venerdì 6 novembre 2026 17:53, Sam <sam@x.com> ha scritto:"): only with an address,
# so a sentence that happens to end in "wrote:" is kept.
_QUOTE_HEADER_LOCAL = re.compile(
    r"^.{0,200}<[^<>\s]+@[^<>\s]+>.{0,40}\b(wrote|ha scritto|a écrit|schrieb|escribió)\s*:\s*$", re.IGNORECASE
)


def strip_quoted_reply(text: str) -> str:
    """Drop '>' quoted lines and everything after an 'On ... wrote:' header (or a localized one with an address).

    Earlier messages are processed on their own; re-reading quoted copies would duplicate evidence.
    """
    out: list[str] = []
    for line in text.splitlines():
        if _QUOTE_HEADER.match(line) or _QUOTE_HEADER_LOCAL.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalized_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject, flags=re.IGNORECASE).strip().lower()


def content_hash(sender: str, subject: str, body: str, attachment_hashes: list[str]) -> str:
    """Stable fingerprint used to recognize a re-sent copy of the same message (fixture E)."""
    h = hashlib.sha256()
    for part in (sender.lower(), _normalized_subject(subject), normalize_ws(body), *sorted(attachment_hashes)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _payload_bytes(part: EmailMessage) -> bytes:
    payload = part.get_payload(decode=True)  # bytes (or None) for a non-multipart part
    return payload if isinstance(payload, bytes) else b""


def _decode_text_part(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        return _payload_bytes(part).decode("utf-8", errors="replace")


def parse_rfc822(
    raw: bytes,
    *,
    provider: Provider,
    message_id: str,
    thread_id: str,
    received_at: str | None = None,
) -> ParsedEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)

    plain: list[str] = []
    html: list[str] = []
    attachments: list[RawAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        ctype = part.get_content_type()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            data = _payload_bytes(part)
            meta = AttachmentMeta(
                attachment_id=f"att-{len(attachments) + 1}",
                filename=(filename or "attachment.bin")[:200],
                mime_type=ctype,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            attachments.append(RawAttachment(meta=meta, data=data))
        elif ctype == "text/plain":
            plain.append(_decode_text_part(part))
        elif ctype == "text/html":
            html.append(_decode_text_part(part))

    body = "\n\n".join(plain) if plain else html_to_text("\n".join(html))
    body = strip_quoted_reply(body.replace("\r\n", "\n"))
    truncated = len(body) > MAX_BODY_CHARS
    body = body[:MAX_BODY_CHARS]

    sender_name, sender_email = parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", "")).strip()[:300]
    if received_at is None:
        try:
            dt = parsedate_to_datetime(str(msg.get("Date")))
            received_at = dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except (TypeError, ValueError):
            received_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    source = SourceMessage(
        provider=provider,
        message_id=message_id,
        thread_id=thread_id,
        rfc_message_id=(str(msg.get("Message-ID")).strip() if msg.get("Message-ID") else None),
        in_reply_to=(str(msg.get("In-Reply-To")).strip() if msg.get("In-Reply-To") else None),
        sender_email=sender_email.lower(),
        sender_name=sender_name or None,
        subject=subject,
        received_at=received_at,
        body_text=body,
        body_truncated=truncated,
        attachments=[a.meta for a in attachments],
        content_hash=content_hash(sender_email, subject, body, [a.meta.sha256 for a in attachments]),
    )
    return ParsedEmail(message=source, attachments=attachments)
