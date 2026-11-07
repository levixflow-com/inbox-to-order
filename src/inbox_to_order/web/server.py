"""Local web API and static host for the React UI.  Start with:  uv run inbox-to-order web

Every email read and every Demo ERP read or write of the app goes through
the two MCP servers (one running pair, kept by `McpSession`); the Demo ERP is written only by
`review.confirm_and_write`, after the operator confirms. The app's own SQLite database is read directly.

The Demo ERP screen (/erp) belongs to the Demo ERP, not to the app: it reads the ERP database read-only.

Security for a browser-facing local API:
- binds to 127.0.0.1;
- every /api and /erp/api request needs the token created at start (handed only to pages served here, which
  other sites cannot read); a custom header also forces a CORS preflight, which this server never approves;
- the Host header must be this server (blocks DNS rebinding), and an Origin header, when present, too.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import db
from ..config import MODEL_PRICES_USD_PER_MTOK, Settings, load_settings
from ..mcp_client import McpSession, Toolbox, ToolCallError
from ..models import AttachmentText, EditedLine, SourceThread
from ..orchestrator import Step, process_thread
from ..review import ReviewError, confirm_and_write, load_choices
from ..services import drafts as drafts_svc
from ..services import erp as erp_svc
from . import views

DIST = Path(__file__).with_name("dist")
TOKEN_PLACEHOLDER = "__API_TOKEN__"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Runtime:
    """Single-user state of one running server: the chosen source and model, the MCP sessions, caches."""

    def __init__(self, base: Settings) -> None:
        self.base = base
        self.mode = base.inbox_mode
        self.extractor = base.resolved_extractor() if base.anthropic_configured else "rules"
        self.model = base.anthropic_model
        self._sessions: dict[str, McpSession] = {}
        self._session_lock = threading.Lock()
        self.threads: dict[tuple[str, str], dict[str, Any]] = {}
        self.listing: dict[str, dict[str, Any]] = {}
        self.last_runs: dict[tuple[str, str], dict[str, Any]] = {}
        # Different conversations can be read at the same time; one conversation is read one run at a time,
        # because each email builds on the proposal left by the one before.
        self.running: set[tuple[str, str]] = set()
        self.running_lock = threading.Lock()

    def start_run(self, key: tuple[str, str]) -> bool:
        with self.running_lock:
            if key in self.running:
                return False
            self.running.add(key)
            return True

    def end_run(self, key: tuple[str, str]) -> None:
        with self.running_lock:
            self.running.discard(key)

    @property
    def settings(self) -> Settings:
        return replace(self.base, inbox_mode=self.mode, extractor=self.extractor, anthropic_model=self.model)

    def conn(self) -> sqlite3.Connection:
        s = self.settings
        if not s.db_path.exists() or not s.erp_db_path.exists():
            db.init_db(s)
        return db.connect(s.db_path)

    def session(self, mode: str) -> McpSession:
        # `mode` is read once by the caller: the inbox can be switched while servers start (several seconds),
        # and a session must be stored under the inbox it was started for.
        with self._session_lock:
            if mode not in self._sessions:
                os.environ["INBOX_MODE"] = mode  # read by the server subprocesses at start
                os.environ["DEMO_ERP_DB"] = str(self.settings.erp_db_path)
                started = McpSession()
                self._sessions[mode] = started
            return self._sessions[mode]

    def mcp(self, fn: Callable[[Toolbox], Coroutine[Any, Any, Any]], mode: str | None = None) -> Any:
        mode = mode or self.mode
        try:
            return self.session(mode).run(fn)
        except (ToolCallError, ReviewError, drafts_svc.DraftError):
            raise
        except Exception as exc:  # a dead server process: start fresh on the next call
            with self._session_lock:
                dead = self._sessions.pop(mode, None)
            if dead is not None:
                dead.close()
            raise ApiError(
                503, "MCP_SESSION_FAILED", f"{type(exc).__name__}. The MCP servers restart on the next action."
            ) from exc

    def needs_ack(self) -> bool:
        return self.extractor == "claude"

    def close(self) -> None:
        for s in self._sessions.values():
            s.close()


# --------------------------------------------------------------------------- request bodies


class SettingsBody(BaseModel):
    inbox_mode: str | None = None
    extractor: str | None = None
    model: str | None = None


class ProcessBody(BaseModel):
    ack: bool = False  # the operator accepted that this email's text is sent to the AI provider


class VersionBody(BaseModel):
    version: int
    note: str = Field("", max_length=300)


class EditBody(BaseModel):
    version: int
    address_id: str
    lines: list[EditedLine]
    note: str = Field("", max_length=300)


# --------------------------------------------------------------------------- app


def create_app(
    base: Settings | None = None, *, token: str | None = None, port: int = 8700, extra_hosts: tuple[str, ...] = ()
) -> FastAPI:
    rt = Runtime(base or load_settings())
    token = token or secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    origins = {f"http://{h}" for h in hosts}

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # noqa: ANN202
        yield
        rt.close()

    app = FastAPI(title="Inbox to Order", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime, app.state.token = rt, token

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"code": "BAD_HOST", "message": "Unknown Host header."}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return JSONResponse(
                {"code": "BAD_ORIGIN", "message": "Requests from other sites are refused."}, status_code=403
            )
        path = request.url.path
        if path.startswith(("/api/", "/erp/api/")) and not hmac.compare_digest(
            request.headers.get("x-api-token", ""), token
        ):
            return JSONResponse({"code": "BAD_TOKEN", "message": "Missing or wrong API token."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(ToolCallError)
    async def _tool_error(_: Request, exc: ToolCallError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=502)

    @app.exception_handler(ReviewError)
    @app.exception_handler(drafts_svc.DraftError)
    async def _review_error(_: Request, exc: Any) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=409)

    # ------------------------------------------------------------------ status and settings

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        s = rt.settings
        session = rt._sessions.get(rt.mode)  # noqa: SLF001 - report only, never starts servers
        models = list(MODEL_PRICES_USD_PER_MTOK)
        if rt.model not in models:
            models.insert(0, rt.model)
        return {
            "inbox_mode": rt.mode,
            "gmail_query": s.gmail_query,
            "extractor": rt.extractor,
            "model": rt.model,
            "models": models,
            "ai_ready": s.anthropic_configured,
            "needs_ack": rt.needs_ack(),
            "mcp_started_seconds": session.started_seconds if session else None,
        }

    @app.post("/api/settings")
    def change_settings(body: SettingsBody) -> dict[str, Any]:
        if body.inbox_mode is not None:
            if body.inbox_mode not in ("fixture", "gmail"):
                raise ApiError(400, "BAD_MODE", "Inbox source must be 'fixture' or 'gmail'.")
            rt.mode = body.inbox_mode
        if body.extractor is not None:
            if body.extractor not in ("claude", "rules") or (
                body.extractor == "claude" and not rt.base.anthropic_configured
            ):
                raise ApiError(400, "BAD_EXTRACTOR", "Claude needs ANTHROPIC_API_KEY; otherwise use 'rules'.")
            rt.extractor = body.extractor
        if body.model is not None:
            if body.model not in MODEL_PRICES_USD_PER_MTOK and body.model != rt.base.anthropic_model:
                raise ApiError(400, "BAD_MODEL", "Unknown model.")
            rt.model = body.model
        return status()

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        # Clears this app's own state and the Demo ERP (both local SQLite files). The mailbox is never touched:
        # in Gmail mode every message simply shows as not read yet again.
        db.init_db(rt.settings, reset=True)
        rt.threads.clear()
        rt.last_runs.clear()
        return {"ok": True}

    # ------------------------------------------------------------------ queue and conversations

    @app.get("/api/queue")
    def queue(refresh: bool = False) -> dict[str, Any]:
        mode = rt.mode
        if refresh or mode not in rt.listing:
            rt.listing[mode] = rt.mcp(lambda box: box.call("list_order_threads", {"limit": 25}), mode)
            if refresh:
                rt.threads = {k: v for k, v in rt.threads.items() if k[0] != mode}
        conn = rt.conn()
        try:
            items = []
            for t in rt.listing[mode]["threads"]:
                lane, reason = views.lane_for(conn, t["provider"], t["thread_id"], t["message_count"])
                items.append(
                    {
                        **t,
                        "lane": lane,
                        "reason": reason,
                        "order": views.order_summary(conn, t["provider"], t["thread_id"]),
                    }
                )
        finally:
            conn.close()
        return {"inbox_mode": mode, "lanes": list(views.LANES), "threads": items}

    def require_listed(thread_id: str, mode: str) -> None:
        """Only conversations of the current inbox's listing are opened (a demo link must not reach Gmail)."""
        for attempt in range(2):
            if attempt or mode not in rt.listing:
                rt.listing[mode] = rt.mcp(lambda box: box.call("list_order_threads", {"limit": 25}), mode)
            if any(t["thread_id"] == thread_id for t in rt.listing[mode]["threads"]):
                return
        source = "the Gmail test inbox" if mode == "gmail" else "the demo emails"
        raise ApiError(
            404,
            "NOT_IN_INBOX",
            f"This conversation is not in {source}, the inbox selected now. " "The link may be from the other inbox.",
        )

    def load_thread(thread_id: str, mode: str) -> tuple[SourceThread, dict[tuple[str, str], AttachmentText]]:
        require_listed(thread_id, mode)
        key = (mode, thread_id)
        if key not in rt.threads:

            async def _load(box: Toolbox) -> dict[str, Any]:
                thread = await box.call("get_order_thread", {"thread_id": thread_id})
                atts = {}
                for m in thread["messages"]:
                    for a in m["attachments"]:
                        if a["mime_type"] == "application/pdf" or a["filename"].lower().endswith(".pdf"):
                            atts[(m["message_id"], a["attachment_id"])] = await box.call(
                                "get_text_attachment",
                                {"message_id": m["message_id"], "attachment_id": a["attachment_id"]},
                            )
                return {"thread": thread, "atts": atts}

            rt.threads[key] = rt.mcp(_load, mode)
        raw = rt.threads[key]
        return (
            SourceThread.model_validate(raw["thread"]),
            {k: AttachmentText.model_validate(v) for k, v in raw["atts"].items()},
        )

    @app.get("/api/threads/{thread_id}")
    def thread(thread_id: str) -> dict[str, Any]:
        mode = rt.mode
        t, atts = load_thread(thread_id, mode)
        conn = rt.conn()
        try:
            out = views.thread_view(conn, t, atts)
        finally:
            conn.close()
        out["last_run"] = rt.last_runs.get((mode, thread_id))
        return out

    @app.post("/api/threads/{thread_id}/process")
    async def process(thread_id: str, body: ProcessBody) -> StreamingResponse:
        if rt.needs_ack() and not body.ack:
            raise ApiError(400, "ACK_REQUIRED", "Confirm that the email text and PDF text are sent to Anthropic first.")
        settings = rt.settings  # one snapshot: inbox, reader and model stay fixed for this run
        mode = settings.inbox_mode
        await asyncio.to_thread(require_listed, thread_id, mode)
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def emit(item: dict[str, Any] | None) -> None:
            loop.call_soon_threadsafe(events.put_nowait, item)

        def on_step(message_id: str, step: Step) -> None:
            emit(
                {
                    "type": "step",
                    "message_id": message_id,
                    "step": {"name": step.name, "status": step.status, "detail": step.detail, "seconds": step.seconds},
                }
            )

        def work() -> None:
            if not rt.start_run((mode, thread_id)):
                emit({"type": "error", "code": "BUSY", "message": "This conversation is already being read."})
                emit(None)
                return
            try:
                conn = rt.conn()
                try:
                    since = views.last_event_id(conn)
                    result = rt.mcp(
                        lambda box: process_thread(settings, thread_id, toolbox=box, max_messages=1, on_step=on_step),
                        mode,
                    )
                    run = views.run_view(result, views.tool_calls_since(conn, thread_id, since))
                finally:
                    conn.close()
                rt.last_runs[(mode, thread_id)] = run
                emit({"type": "done", "run": run})
            except (ToolCallError, ApiError) as exc:
                emit({"type": "error", "code": exc.code, "message": exc.message})
            except Exception as exc:  # noqa: BLE001 - reported to the page, which shows it
                emit({"type": "error", "code": "RUN_FAILED", "message": type(exc).__name__})
            finally:
                rt.end_run((mode, thread_id))
                emit(None)

        async def stream():  # noqa: ANN202
            yield json.dumps({"type": "started", "extractor": settings.resolved_extractor()}) + "\n"
            task = asyncio.create_task(asyncio.to_thread(work))
            while (item := await events.get()) is not None:
                yield json.dumps(item) + "\n"
            await task

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # ------------------------------------------------------------------ operator decisions

    @app.get("/api/drafts/{draft_id}/choices")
    def choices(draft_id: int) -> dict[str, Any]:
        conn = rt.conn()
        try:
            d = drafts_svc.get_draft_by_id(conn, draft_id)
        finally:
            conn.close()
        if d is None:
            raise ApiError(404, "DRAFT_NOT_FOUND", f"No proposal {draft_id}.")
        products, customer = rt.mcp(lambda box: load_choices(box, d.customer_id))
        return {
            "products": [p.model_dump() for p in products.products],
            "addresses": [a.model_dump() for a in (customer.customer.addresses if customer.customer else [])],
        }

    @app.post("/api/drafts/{draft_id}/edit")
    def edit(draft_id: int, body: EditBody) -> dict[str, Any]:
        conn = rt.conn()
        try:
            d = drafts_svc.get_draft_by_id(conn, draft_id)
            if d is None:
                raise ApiError(404, "DRAFT_NOT_FOUND", f"No proposal {draft_id}.")
            products, customer = rt.mcp(lambda box: load_choices(box, d.customer_id))
            addresses = {a.address_id: a for a in (customer.customer.addresses if customer.customer else [])}
            if body.address_id not in addresses:
                raise ApiError(400, "UNKNOWN_ADDRESS", "Choose one of the customer's addresses.")
            saved = drafts_svc.operator_edit(
                conn,
                draft_id,
                body.version,
                ship_to=addresses[body.address_id],
                lines=body.lines,
                catalog={p.sku: p for p in products.products},
                note=body.note,
            )
            return views.draft_view(conn, saved)
        finally:
            conn.close()

    @app.post("/api/drafts/{draft_id}/confirm")
    def confirm(draft_id: int, body: VersionBody) -> dict[str, Any]:
        db_path = rt.settings.db_path

        async def _confirm(box: Toolbox) -> Any:
            c = db.connect(db_path)  # runs on the MCP session thread
            try:
                return await confirm_and_write(c, box, draft_id, body.version)
            finally:
                c.close()

        written = rt.mcp(_confirm)
        return {
            "outcome": written.outcome,
            "order_id": written.order.order_id,
            "version": written.order.version,
            "changes": written.changes,
        }

    @app.post("/api/drafts/{draft_id}/reject")
    def reject(draft_id: int, body: VersionBody) -> dict[str, Any]:
        conn = rt.conn()
        try:
            return views.draft_view(conn, drafts_svc.reject_draft(conn, draft_id, body.version, body.note))
        finally:
            conn.close()

    # ------------------------------------------------------------------ Demo ERP screen (read-only, its own data)

    @app.get("/erp/api/orders")
    def erp_orders() -> dict[str, Any]:
        path = rt.settings.erp_db_path
        if not path.exists():
            return {"orders": [], "last_change": None}
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            orders = []
            for o in erp_svc.list_orders(conn, limit=100).orders:
                hist = [
                    {
                        "version": h["version"],
                        "action": h["action"],
                        "changes": json.loads(h["changes"] or "[]"),
                        "source_note": h["source_note"],
                        "at": h["at"],
                    }
                    for h in erp_svc.order_history(conn, o.order_id)
                ]
                orders.append({**o.model_dump(), "history": hist})
        finally:
            conn.close()
        return {"orders": orders, "last_change": max((o["updated_at"] for o in orders), default=None)}

    # ------------------------------------------------------------------ pages

    def page(name: str) -> HTMLResponse:
        f = DIST / name
        if not f.exists():
            return HTMLResponse(
                "<p>The web UI is not built. Run scripts/build-ui.sh (see the README).</p>", status_code=503
            )
        return HTMLResponse(
            f.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, token), headers={"Cache-Control": "no-store"}
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return page("index.html")

    @app.get("/erp", response_class=HTMLResponse)
    def erp_page() -> HTMLResponse:
        return page("erp.html")

    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    if rt.extractor == "claude":
        # Importing the Anthropic SDK takes several seconds the first time; do it before the first run.
        threading.Thread(target=lambda: __import__("inbox_to_order.agent"), daemon=True).start()

    return app


def serve(port: int = 8700) -> None:
    import uvicorn

    uvicorn.run(create_app(port=port), host="127.0.0.1", port=port, log_level="warning")
