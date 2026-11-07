"""Two inbox providers behind one interface: local .eml fixtures and read-only Gmail.

Both produce the same normalized `SourceThread`/`SourceMessage` types via the same MIME parser,
so the MCP server, the orchestrator and the UI run identical code in both modes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from ..config import MAX_MESSAGES_PER_THREAD, MAX_THREADS_LISTED, Settings
from ..models import SourceThread, ThreadList, ThreadSummary
from .email_parser import ParsedEmail, RawAttachment, b64url_decode, parse_rfc822
from .gmail_auth import GmailAuthError, load_credentials


class InboxError(RuntimeError):
    """Predictable provider failure with a user-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class InboxSource(Protocol):
    provider: str

    def list_threads(self, query: str | None, limit: int) -> ThreadList: ...
    def get_thread(self, thread_id: str) -> SourceThread: ...
    def get_attachment(self, message_id: str, attachment_id: str) -> RawAttachment: ...


def _summary(thread: SourceThread) -> ThreadSummary:
    last = thread.messages[-1]
    return ThreadSummary(
        provider=thread.provider,
        thread_id=thread.thread_id,
        subject=thread.subject,
        sender_email=thread.messages[0].sender_email,
        last_received_at=last.received_at,
        message_count=len(thread.messages),
        attachment_count=sum(len(m.attachments) for m in thread.messages),
    )


# --------------------------------------------------------------------------- fixtures


class FixtureInbox:
    """Reads fixtures/messages/*.eml. Message id = file stem; threads follow In-Reply-To/References."""

    provider = "fixture"

    def __init__(self, messages_dir: Path) -> None:
        self.messages_dir = messages_dir
        self._parsed: dict[str, ParsedEmail] = {}
        self._threads: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        raw_by_id = {p.stem: p.read_bytes() for p in sorted(self.messages_dir.glob("*.eml"))}
        # First pass: parse with a placeholder thread id to learn RFC ids and reply links.
        first = {
            mid: parse_rfc822(raw, provider="fixture", message_id=mid, thread_id="?") for mid, raw in raw_by_id.items()
        }
        rfc_to_mid = {p.message.rfc_message_id: mid for mid, p in first.items() if p.message.rfc_message_id}

        def root(mid: str, depth: int = 0) -> str:
            parent = first[mid].message.in_reply_to
            if parent and parent in rfc_to_mid and depth < 20:
                return root(rfc_to_mid[parent], depth + 1)
            return mid

        for mid, raw in raw_by_id.items():
            tid = f"thread-{root(mid)}"
            self._parsed[mid] = parse_rfc822(raw, provider="fixture", message_id=mid, thread_id=tid)
            self._threads.setdefault(tid, []).append(mid)
        for mids in self._threads.values():
            mids.sort(key=lambda m: self._parsed[m].message.received_at)

    def list_threads(self, query: str | None, limit: int) -> ThreadList:
        limit = max(1, min(limit, MAX_THREADS_LISTED))
        threads = [self.get_thread(tid) for tid in self._threads]
        if query:
            q = query.lower()
            threads = [t for t in threads if any(q in m.subject.lower() or q in m.sender_email for m in t.messages)]
        threads.sort(key=lambda t: t.messages[0].received_at)
        return ThreadList(provider="fixture", threads=[_summary(t) for t in threads[:limit]])

    def get_thread(self, thread_id: str) -> SourceThread:
        mids = self._threads.get(thread_id)
        if not mids:
            raise InboxError("THREAD_NOT_FOUND", f"No fixture thread {thread_id!r}.")
        msgs = [self._parsed[m].message for m in mids[:MAX_MESSAGES_PER_THREAD]]
        return SourceThread(provider="fixture", thread_id=thread_id, subject=msgs[0].subject, messages=msgs)

    def get_attachment(self, message_id: str, attachment_id: str) -> RawAttachment:
        parsed = self._parsed.get(message_id)
        if parsed is None:
            raise InboxError("MESSAGE_NOT_FOUND", f"No fixture message {message_id!r}.")
        for att in parsed.attachments:
            if att.meta.attachment_id == attachment_id:
                return att
        raise InboxError("ATTACHMENT_NOT_FOUND", f"Message {message_id!r} has no attachment {attachment_id!r}.")


# --------------------------------------------------------------------------- Gmail (read-only)


class GmailInbox:
    """Gmail API, gmail.readonly scope. Only list/get calls exist in this class - no send/modify/delete."""

    provider = "gmail"

    def __init__(self, settings: Settings, service=None) -> None:  # noqa: ANN001
        self.settings = settings
        self._service = service
        self._cache: dict[str, ParsedEmail] = {}

    @property
    def service(self):  # noqa: ANN201
        if self._service is None:
            from googleapiclient.discovery import build

            creds = load_credentials(self.settings)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _execute(self, request):  # noqa: ANN001, ANN202
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=1)
        except RefreshError as exc:
            raise GmailAuthError(
                "The Gmail authorization expired or was revoked. Run `uv run inbox-to-order gmail-auth`."
            ) from exc
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (401, 403):
                raise GmailAuthError(
                    f"Gmail refused the request (HTTP {status}). Re-authorize with `uv run inbox-to-order gmail-auth`."
                ) from exc
            if status == 404:
                raise InboxError("NOT_FOUND", "Gmail could not find that thread or message.") from exc
            raise InboxError("GMAIL_HTTP_ERROR", f"Gmail API error (HTTP {status}).") from exc
        except OSError as exc:
            raise InboxError("NETWORK_ERROR", f"Network error talking to Gmail ({type(exc).__name__}).") from exc

    def list_threads(self, query: str | None, limit: int) -> ThreadList:
        limit = max(1, min(limit, MAX_THREADS_LISTED))
        users = self.service.users()
        resp = self._execute(users.threads().list(userId="me", q=query or self.settings.gmail_query, maxResults=limit))
        out: list[ThreadSummary] = []
        for t in resp.get("threads", [])[:limit]:
            # format="metadata" returns headers only, so attachments could not be counted (found in the
            # first live Gmail run). "full" with a field mask returns headers and part filenames, not bodies.
            meta = self._execute(users.threads().get(userId="me", id=t["id"], format="full", fields=_LIST_FIELDS))
            msgs = meta.get("messages", [])
            if not msgs:
                continue
            headers = {h["name"].lower(): h["value"] for h in msgs[0].get("payload", {}).get("headers", [])}

            out.append(
                ThreadSummary(
                    provider="gmail",
                    thread_id=t["id"],
                    subject=headers.get("subject", "(no subject)")[:300],
                    sender_email=parseaddr(headers.get("from", ""))[1].lower(),
                    last_received_at=_ms_to_iso(msgs[-1].get("internalDate")),
                    message_count=len(msgs),
                    attachment_count=sum(_count_attachments(m.get("payload", {})) for m in msgs),
                )
            )
        return ThreadList(provider="gmail", threads=out)

    def _get_message(self, message_id: str, thread_id: str | None = None) -> ParsedEmail:
        if message_id in self._cache:
            return self._cache[message_id]
        resp = self._execute(self.service.users().messages().get(userId="me", id=message_id, format="raw"))
        raw = b64url_decode(resp["raw"])
        parsed = parse_rfc822(
            raw,
            provider="gmail",
            message_id=resp["id"],
            thread_id=thread_id or resp["threadId"],
            received_at=_ms_to_iso(resp.get("internalDate")),
        )
        self._cache[message_id] = parsed
        return parsed

    def get_thread(self, thread_id: str) -> SourceThread:
        resp = self._execute(self.service.users().threads().get(userId="me", id=thread_id, format="minimal"))
        ids = [m["id"] for m in resp.get("messages", [])][:MAX_MESSAGES_PER_THREAD]
        if not ids:
            raise InboxError("THREAD_NOT_FOUND", f"Gmail thread {thread_id!r} has no messages.")
        msgs = sorted((self._get_message(i, thread_id).message for i in ids), key=lambda m: m.received_at)
        return SourceThread(provider="gmail", thread_id=thread_id, subject=msgs[0].subject, messages=msgs)

    def get_attachment(self, message_id: str, attachment_id: str) -> RawAttachment:
        parsed = self._get_message(message_id)
        for att in parsed.attachments:
            if att.meta.attachment_id == attachment_id:
                return att
        raise InboxError("ATTACHMENT_NOT_FOUND", f"Message {message_id!r} has no attachment {attachment_id!r}.")


def _ms_to_iso(ms: str | int | None) -> str:
    if not ms:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).replace(microsecond=0).isoformat()


_PART = "filename,parts(filename,parts(filename,parts(filename)))"
_LIST_FIELDS = f"messages(internalDate,payload(headers,{_PART}))"


def _count_attachments(payload: dict) -> int:
    n = 1 if payload.get("filename") else 0
    return n + sum(_count_attachments(p) for p in payload.get("parts", []) or [])


def make_inbox(settings: Settings) -> InboxSource:
    if settings.inbox_mode == "gmail":
        return GmailInbox(settings)
    return FixtureInbox(settings.fixtures_dir / "messages")
