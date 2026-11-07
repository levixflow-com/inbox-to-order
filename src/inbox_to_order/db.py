"""The app's own SQLite database: proposals awaiting review, their revisions, the processing log and
the audit trail. (The Demo ERP has its own database, owned by services/erp.py.)

Uniqueness constraints carry the important guarantees (one proposal per customer+PO, one processing
record per source message, one revision per proposal version), so idempotency does not depend on
prompt wording or on the UI behaving.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_drafts (
    draft_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       TEXT NOT NULL,              -- Demo ERP customer id
    customer_name     TEXT NOT NULL,
    po_number         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('needs_review','ready','confirmed','rejected')),
    version           INTEGER NOT NULL DEFAULT 1,
    source_thread_id  TEXT NOT NULL,
    ship_to           TEXT NOT NULL,              -- JSON ShipToIn
    issues            TEXT NOT NULL DEFAULT '[]', -- JSON list[Issue]
    erp_order_id      TEXT,                       -- set once written to (or copied from) the Demo ERP
    erp_version       INTEGER,                    -- the ERP version this proposal is based on / produced
    confirmed_version INTEGER,                    -- proposal version the operator confirmed last
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (customer_id, po_number)
);
CREATE TABLE IF NOT EXISTS draft_lines (
    draft_id          INTEGER NOT NULL REFERENCES order_drafts(draft_id),
    line_no           INTEGER NOT NULL,
    customer_item_code TEXT,
    description       TEXT,
    raw_quantity_text TEXT,
    sku               TEXT,
    product_name      TEXT,
    quantity          INTEGER CHECK (quantity IS NULL OR quantity > 0),
    unit              TEXT,
    unit_price        TEXT,
    status            TEXT NOT NULL,
    note              TEXT,
    evidence          TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (draft_id, line_no)
);
CREATE TABLE IF NOT EXISTS draft_revisions (
    draft_id          INTEGER NOT NULL REFERENCES order_drafts(draft_id),
    version           INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('created','copied_from_erp','amended','edited')),
    actor             TEXT NOT NULL,              -- assistant | operator | demo-erp
    source_message_id TEXT,
    changes           TEXT NOT NULL DEFAULT '[]', -- JSON list[ChangeOut] with before/after
    created_at        TEXT NOT NULL,
    PRIMARY KEY (draft_id, version)
);
CREATE TABLE IF NOT EXISTS processed_sources (
    provider          TEXT NOT NULL,
    message_id        TEXT NOT NULL,
    thread_id         TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    outcome           TEXT NOT NULL,              -- created | amended | needs_review | duplicate | no_action
    draft_id          INTEGER,
    version           INTEGER,
    detail            TEXT NOT NULL DEFAULT '{}',
    processed_at      TEXT NOT NULL,
    PRIMARY KEY (provider, message_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    actor             TEXT NOT NULL,              -- app | model | operator
    event             TEXT NOT NULL,
    thread_id         TEXT,
    message_id        TEXT,
    draft_id          INTEGER,
    detail            TEXT NOT NULL DEFAULT '{}'  -- redacted, bounded
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # autocommit; explicit BEGIN below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def clear_database(db_path: Path) -> None:
    """Empty a database in place. Deleting the file fails on Windows while the UI or an MCP server still
    holds it open (WinError 32), so the tables are dropped instead."""
    if not db_path.exists():
        return
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        tables = [
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        with transaction(conn):
            for name in tables:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                conn.execute("DELETE FROM sqlite_sequence")
    finally:
        conn.close()


def init_app_db(db_path: Path, *, reset: bool = False) -> None:
    """Create the app's own working-state database (proposals, revisions, processing log, audit)."""
    if reset:
        clear_database(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def init_db(settings: Settings, *, reset: bool = False) -> None:
    """Create/seed both local databases: the app's working state and the fictional Demo ERP."""
    from .services.erp import init_erp_db

    init_app_db(settings.db_path, reset=reset)
    init_erp_db(settings.erp_db_path, settings.fixtures_dir, reset=reset, extra_senders=settings.extra_customer_senders)


# --------------------------------------------------------------------------- app-owned helpers


_SAFE_DETAIL_KEYS = {
    "tool",
    "server",
    "status",
    "outcome",
    "error_code",
    "po_number",
    "customer_id",
    "sku",
    "query",
    "match_type",
    "candidates",
    "version",
    "line_count",
    "changes",
    "reason",
    "extractor",
    "attachment_id",
    "pages",
    "rounds",
    "model",
    "note",
    "duplicate_of",
    "issues",
    "input_tokens",
    "output_tokens",
    "limit",
    "order_id",
    "erp_version",
    "seconds",
    "cost_usd",
    "error",
}


def redact(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep only allow-listed keys and short values: no bodies, no tokens, no addresses in logs."""
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if k not in _SAFE_DETAIL_KEYS:
            continue
        if isinstance(v, str) and len(v) > 160:
            v = v[:157] + "..."
        out[k] = v
    return out


def audit(
    conn: sqlite3.Connection,
    actor: str,
    event: str,
    *,
    thread_id: str | None = None,
    message_id: str | None = None,
    draft_id: int | None = None,
    **detail: Any,
) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts,actor,event,thread_id,message_id,draft_id,detail) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor, event, thread_id, message_id, draft_id, json.dumps(redact(detail), default=str)),
    )


def processed_record(conn: sqlite3.Connection, provider: str, message_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM processed_sources WHERE provider=? AND message_id=?", (provider, message_id)
    ).fetchone()


def find_processed_by_hash(conn: sqlite3.Connection, provider: str, content_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM processed_sources WHERE provider=? AND content_hash=? AND outcome IN ('created','amended') "
        "ORDER BY processed_at LIMIT 1",
        (provider, content_hash),
    ).fetchone()


def record_processed(
    conn: sqlite3.Connection,
    *,
    provider: str,
    message_id: str,
    thread_id: str,
    content_hash: str,
    outcome: str,
    draft_id: int | None = None,
    version: int | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Insert a processing record. Returns False if the message was already recorded (idempotent)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO processed_sources(provider,message_id,thread_id,content_hash,outcome,draft_id,version,detail,processed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            provider,
            message_id,
            thread_id,
            content_hash,
            outcome,
            draft_id,
            version,
            json.dumps(detail or {}),
            now_iso(),
        ),
    )
    return cur.rowcount == 1


def list_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM order_drafts ORDER BY updated_at DESC, draft_id DESC").fetchall()


def list_revisions(conn: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM draft_revisions WHERE draft_id=? ORDER BY version", (draft_id,)).fetchall()


def list_audit(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()


def list_processed(conn: sqlite3.Connection, thread_id: str | None = None) -> list[sqlite3.Row]:
    if thread_id is None:
        return conn.execute("SELECT * FROM processed_sources ORDER BY processed_at").fetchall()
    return conn.execute(
        "SELECT * FROM processed_sources WHERE thread_id=? ORDER BY processed_at", (thread_id,)
    ).fetchall()
