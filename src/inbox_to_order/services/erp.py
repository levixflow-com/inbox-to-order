"""The Demo ERP: a deliberately small, fictional order system behind the Demo ERP MCP server.

It stands in for the business system a company already has. It owns customers, ship-to addresses,
the catalog, customer item codes and confirmed orders, and it enforces its own rules on every write
(known customer, address ownership, existing SKU, selling unit, quantity range, unique PO per
customer, optimistic version, idempotency key) without trusting the caller.

To connect a real ERP, you would replace this module and the Demo ERP MCP server with an adapter
that exposes the same tools (see docs/EXTENDING.md). Nothing else in the app reads this database.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ..config import MAX_LINE_QUANTITY
from ..db import clear_database, connect, now_iso, transaction
from ..models import (
    AddressOut,
    CreateOrderRequest,
    CustomerLookup,
    CustomerOut,
    ErpOrder,
    ErpOrderLine,
    OrderLineIn,
    OrderList,
    OrderLookup,
    OrderWriteResult,
    ProductList,
    ProductLookup,
    ProductOut,
    UpdateOrderRequest,
)
from .text import tokens

PO_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,30}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    sender_emails   TEXT NOT NULL DEFAULT '[]',   -- JSON list, lower-case
    sender_domains  TEXT NOT NULL DEFAULT '[]'    -- JSON list, lower-case
);
CREATE TABLE IF NOT EXISTS customer_addresses (
    address_id      TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    label           TEXT NOT NULL,
    line1           TEXT NOT NULL,
    line2           TEXT,
    city            TEXT NOT NULL,
    state           TEXT NOT NULL,
    zip             TEXT NOT NULL CHECK (length(zip) = 5),
    country         TEXT NOT NULL DEFAULT 'USA',
    is_default      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    unit            TEXT NOT NULL,                -- selling unit: case | box | roll
    unit_label      TEXT NOT NULL,                -- e.g. 'case (24 rolls)'
    unit_price      TEXT,                         -- fictional demo price, decimal string
    keywords        TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS customer_item_codes (
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    customer_code   TEXT NOT NULL,                -- upper-case
    sku             TEXT NOT NULL REFERENCES products(sku),
    PRIMARY KEY (customer_id, customer_code)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,             -- e.g. SO-1001
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    po_number       TEXT NOT NULL,
    ship_to_address_id TEXT NOT NULL REFERENCES customer_addresses(address_id),
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (customer_id, po_number)
);
CREATE TABLE IF NOT EXISTS order_lines (
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    line_no         INTEGER NOT NULL,
    sku             TEXT NOT NULL REFERENCES products(sku),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    unit            TEXT NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
CREATE TABLE IF NOT EXISTS order_history (
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    version         INTEGER NOT NULL,
    action          TEXT NOT NULL,                -- created | updated
    changes         TEXT NOT NULL DEFAULT '[]',
    source_note     TEXT,
    at              TEXT NOT NULL,
    PRIMARY KEY (order_id, version)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    version         INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    at              TEXT NOT NULL
);
"""


class ErpError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- setup


def init_erp_db(db_path: Path, fixtures_dir: Path, *, reset: bool = False, extra_senders: str = "") -> None:
    """Create the Demo ERP and load its fictional master data and seed orders from fixtures/."""
    if reset:
        clear_database(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with transaction(conn):
            _seed(conn, fixtures_dir, extra_senders)
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection, fixtures_dir: Path, extra_senders: str) -> None:
    with open(fixtures_dir / "catalog.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            conn.execute(
                "INSERT OR REPLACE INTO products(sku,name,unit,unit_label,unit_price,keywords) VALUES (?,?,?,?,?,?)",
                (
                    row["sku"],
                    row["name"],
                    row["unit"],
                    row["unit_label"],
                    row["unit_price"] or None,
                    json.dumps([k.strip().lower() for k in row["keywords"].split(";") if k.strip()]),
                ),
            )
    data = json.loads((fixtures_dir / "customers.json").read_text(encoding="utf-8"))
    extra = parse_extra_senders(extra_senders)
    for c in data["customers"]:
        emails = [e.lower() for e in c.get("sender_emails", [])] + extra.get(c["customer_id"], [])
        conn.execute(
            "INSERT OR REPLACE INTO customers(customer_id,name,sender_emails,sender_domains) VALUES (?,?,?,?)",
            (
                c["customer_id"],
                c["name"],
                json.dumps(emails),
                json.dumps([d.lower() for d in c.get("sender_domains", [])]),
            ),
        )
        for a in c["addresses"]:
            conn.execute(
                "INSERT OR REPLACE INTO customer_addresses VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    a["address_id"],
                    c["customer_id"],
                    a["label"],
                    a["line1"],
                    a.get("line2"),
                    a["city"],
                    a["state"],
                    a["zip"],
                    a.get("country", "USA"),
                    int(a.get("is_default", False)),
                ),
            )
        for code, sku in c.get("item_codes", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO customer_item_codes(customer_id,customer_code,sku) VALUES (?,?,?)",
                (c["customer_id"], code.upper(), sku),
            )
    orders_file = fixtures_dir / "erp_orders.json"
    if orders_file.exists():
        for o in json.loads(orders_file.read_text(encoding="utf-8"))["orders"]:
            if conn.execute("SELECT 1 FROM orders WHERE order_id=?", (o["order_id"],)).fetchone():
                continue  # keep any changes made since seeding
            ts = o["created_at"]
            conn.execute(
                "INSERT INTO orders(order_id,customer_id,po_number,ship_to_address_id,version,created_at,updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (o["order_id"], o["customer_id"], o["po_number"], o["ship_to_address_id"], ts, ts),
            )
            for n, (sku, qty, unit) in enumerate(o["lines"], start=1):
                conn.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)", (o["order_id"], n, sku, qty, unit))
            conn.execute(
                "INSERT INTO order_history(order_id,version,action,changes,source_note,at) VALUES (?,1,'created','[]',?,?)",
                (o["order_id"], "seed data (fictional)", ts),
            )


def parse_extra_senders(value: str) -> dict[str, list[str]]:
    """EXTRA_CUSTOMER_SENDERS="PINE-RIDGE=me@gmail.com,LAKESHORE=other@example.com" (for your Gmail test)."""
    out: dict[str, list[str]] = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        if "=" not in part:
            raise ValueError(f"EXTRA_CUSTOMER_SENDERS entry must be CUSTOMER_ID=email, got {part!r}")
        cid, email = part.split("=", 1)
        out.setdefault(cid.strip(), []).append(email.strip().lower())
    return out


# --------------------------------------------------------------------------- customers


def format_address(row: sqlite3.Row) -> str:
    street = ", ".join(p for p in (row["line1"], row["line2"]) if p)
    return f"{street}, {row['city']}, {row['state']} {row['zip']}, {row['country']}"


def _address_out(r: sqlite3.Row) -> AddressOut:
    return AddressOut(
        address_id=r["address_id"],
        label=r["label"],
        one_line=format_address(r),
        city=r["city"],
        state=r["state"],
        zip=r["zip"],
        is_default=bool(r["is_default"]),
    )


def _customer_out(conn: sqlite3.Connection, row: sqlite3.Row) -> CustomerOut:
    rows = conn.execute(
        "SELECT * FROM customer_addresses WHERE customer_id=? ORDER BY is_default DESC, address_id",
        (row["customer_id"],),
    ).fetchall()
    return CustomerOut(customer_id=row["customer_id"], name=row["name"], addresses=[_address_out(r) for r in rows])


def find_customer(conn: sqlite3.Connection, sender_or_name: str) -> CustomerLookup:
    """Explicit mappings only: exact sender email, then sender domain, then exact legal name."""
    q = sender_or_name.strip().lower()
    rows = conn.execute("SELECT * FROM customers ORDER BY customer_id").fetchall()
    if "@" in q:
        for r in rows:
            if q in json.loads(r["sender_emails"]):
                return CustomerLookup(status="matched", match_type="sender_email", customer=_customer_out(conn, r))
        domain = q.rsplit("@", 1)[1]
        hits = [r for r in rows if domain in json.loads(r["sender_domains"])]
        if len(hits) == 1:
            return CustomerLookup(status="matched", match_type="sender_domain", customer=_customer_out(conn, hits[0]))
        return CustomerLookup(status="not_found")
    hits = [r for r in rows if r["name"].lower() == q]
    if len(hits) == 1:
        return CustomerLookup(status="matched", match_type="exact_name", customer=_customer_out(conn, hits[0]))
    return CustomerLookup(status="not_found")


def get_customer(conn: sqlite3.Connection, customer_id: str) -> CustomerLookup:
    row = conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if row is None:
        return CustomerLookup(status="not_found")
    return CustomerLookup(status="matched", match_type="customer_id", customer=_customer_out(conn, row))


def _require_customer(conn: sqlite3.Connection, customer_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if row is None:
        raise ErpError("UNKNOWN_CUSTOMER", f"No customer {customer_id!r} in the Demo ERP.")
    return row


# --------------------------------------------------------------------------- catalog


def _item_codes(conn: sqlite3.Connection, customer_id: str) -> dict[str, str]:
    """sku -> the customer's own item code."""
    return {
        r["sku"]: r["customer_code"]
        for r in conn.execute("SELECT * FROM customer_item_codes WHERE customer_id=?", (customer_id,)).fetchall()
    }


def _product_out(row: sqlite3.Row, code: str | None) -> ProductOut:
    return ProductOut(
        sku=row["sku"],
        name=row["name"],
        unit=row["unit"],
        unit_label=row["unit_label"],
        customer_item_code=code,
        unit_price=row["unit_price"],
    )


def find_product(conn: sqlite3.Connection, customer_id: str, query: str) -> ProductLookup:
    """Customer item code -> internal SKU -> description words. Never silently picks among several."""
    _require_customer(conn, customer_id)
    q = query.strip()
    if not q:
        return ProductLookup(status="not_found", query=query)
    codes = _item_codes(conn, customer_id)
    row = conn.execute(
        "SELECT p.* FROM customer_item_codes a JOIN products p USING(sku) WHERE a.customer_id=? AND a.customer_code=?",
        (customer_id, q.upper()),
    ).fetchone()
    if row:
        return ProductLookup(
            status="matched", match_type="customer_alias", query=q, candidates=[_product_out(row, q.upper())]
        )
    row = conn.execute("SELECT * FROM products WHERE sku=?", (q.upper(),)).fetchone()
    if row:
        return ProductLookup(
            status="matched", match_type="internal_sku", query=q, candidates=[_product_out(row, codes.get(row["sku"]))]
        )
    qt = tokens(q)
    if not qt:
        return ProductLookup(status="not_found", query=q)
    hits = []
    for r in conn.execute("SELECT * FROM products ORDER BY sku").fetchall():
        pt = tokens(r["name"]) | {t for k in json.loads(r["keywords"]) for t in tokens(k)}
        if qt <= pt:
            hits.append(_product_out(r, codes.get(r["sku"])))
    if not hits:
        return ProductLookup(status="not_found", query=q)
    return ProductLookup(
        status="matched" if len(hits) == 1 else "ambiguous", match_type="description", query=q, candidates=hits[:10]
    )


def list_products(conn: sqlite3.Connection, customer_id: str) -> ProductList:
    _require_customer(conn, customer_id)
    codes = _item_codes(conn, customer_id)
    rows = conn.execute("SELECT * FROM products ORDER BY sku").fetchall()
    return ProductList(customer_id=customer_id, products=[_product_out(r, codes.get(r["sku"])) for r in rows])


def line_amount(price: str | None, quantity: int | None) -> Decimal | None:
    if price is None or quantity is None:
        return None
    return (Decimal(price) * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- orders (read)


def _order_out(conn: sqlite3.Connection, row: sqlite3.Row) -> ErpOrder:
    codes = _item_codes(conn, row["customer_id"])
    lines = []
    total = Decimal("0")
    for r in conn.execute(
        "SELECT l.*, p.name, p.unit_price FROM order_lines l JOIN products p USING(sku) WHERE order_id=? ORDER BY line_no",
        (row["order_id"],),
    ).fetchall():
        amount = line_amount(r["unit_price"], r["quantity"])
        total += amount or 0
        lines.append(
            ErpOrderLine(
                line_no=r["line_no"],
                sku=r["sku"],
                name=r["name"],
                quantity=r["quantity"],
                unit=r["unit"],
                unit_price=r["unit_price"],
                customer_item_code=codes.get(r["sku"]),
            )
        )
    addr = conn.execute("SELECT * FROM customer_addresses WHERE address_id=?", (row["ship_to_address_id"],)).fetchone()
    name = conn.execute("SELECT name FROM customers WHERE customer_id=?", (row["customer_id"],)).fetchone()["name"]
    return ErpOrder(
        order_id=row["order_id"],
        customer_id=row["customer_id"],
        customer_name=name,
        po_number=row["po_number"],
        version=row["version"],
        ship_to=_address_out(addr),
        lines=lines,
        total_usd=f"{total:.2f}",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_order(conn: sqlite3.Connection, customer_id: str, po_number: str) -> OrderLookup:
    _require_customer(conn, customer_id)
    row = conn.execute(
        "SELECT * FROM orders WHERE customer_id=? AND po_number=?", (customer_id, po_number.strip().upper())
    ).fetchone()
    return OrderLookup(found=row is not None, order=_order_out(conn, row) if row else None)


def get_order_by_id(conn: sqlite3.Connection, order_id: str) -> ErpOrder | None:
    row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return _order_out(conn, row) if row else None


def list_orders(conn: sqlite3.Connection, limit: int = 50) -> OrderList:
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY updated_at DESC, order_id DESC LIMIT ?", (max(1, min(limit, 200)),)
    ).fetchall()
    return OrderList(orders=[_order_out(conn, r) for r in rows])


def order_history(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM order_history WHERE order_id=? ORDER BY version", (order_id,)).fetchall()


# --------------------------------------------------------------------------- orders (write)


def _validate(conn: sqlite3.Connection, customer_id: str, ship_to_address_id: str, lines: list[OrderLineIn]) -> None:
    if not conn.execute(
        "SELECT 1 FROM customer_addresses WHERE address_id=? AND customer_id=?", (ship_to_address_id, customer_id)
    ).fetchone():
        raise ErpError("ADDRESS_NOT_ALLOWED", f"Address {ship_to_address_id!r} is not on file for {customer_id}.")
    if not lines:
        raise ErpError("NO_LINES", "An order needs at least one line.")
    seen: set[str] = set()
    for ln in lines:
        prod = conn.execute("SELECT * FROM products WHERE sku=?", (ln.sku,)).fetchone()
        if prod is None:
            raise ErpError("UNKNOWN_SKU", f"SKU {ln.sku!r} does not exist.")
        if ln.unit != prod["unit"]:
            raise ErpError(
                "UNIT_MISMATCH", f"{ln.sku} sells by '{prod['unit']}', not '{ln.unit}'. The ERP does not convert units."
            )
        if not (1 <= ln.quantity <= MAX_LINE_QUANTITY):
            raise ErpError("INVALID_QUANTITY", f"Quantity {ln.quantity} for {ln.sku} is outside 1-{MAX_LINE_QUANTITY}.")
        if ln.sku in seen:
            raise ErpError("DUPLICATE_SKU", f"{ln.sku} appears on more than one line; combine them.")
        seen.add(ln.sku)


def _request_hash(payload: str) -> str:
    import hashlib

    return hashlib.sha256(payload.encode()).hexdigest()


def _replay(conn: sqlite3.Connection, key: str, request_hash: str) -> OrderWriteResult | None:
    row = conn.execute("SELECT * FROM idempotency_keys WHERE idempotency_key=?", (key,)).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ErpError("IDEMPOTENCY_KEY_REUSED", "This idempotency key was already used for a different request.")
    order = get_order_by_id(conn, row["order_id"])
    assert order is not None
    return OrderWriteResult(
        outcome="replay", order=order, changes=[f"Already applied as version {row['version']}; nothing changed."]
    )


def _write_lines(conn: sqlite3.Connection, order_id: str, lines: list[OrderLineIn]) -> None:
    conn.execute("DELETE FROM order_lines WHERE order_id=?", (order_id,))
    for n, ln in enumerate(lines, start=1):
        conn.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)", (order_id, n, ln.sku, ln.quantity, ln.unit))


def create_order(conn: sqlite3.Connection, req: CreateOrderRequest) -> OrderWriteResult:
    po = req.po_number.strip().upper()
    if not PO_PATTERN.match(po):
        raise ErpError("INVALID_PO", f"PO number {req.po_number!r} is not a valid identifier.")
    rhash = _request_hash("create:" + req.model_dump_json(exclude={"source_note"}))
    with transaction(conn):
        replay = _replay(conn, req.idempotency_key, rhash)
        if replay:
            return replay
        _require_customer(conn, req.customer_id)
        existing = conn.execute(
            "SELECT order_id FROM orders WHERE customer_id=? AND po_number=?", (req.customer_id, po)
        ).fetchone()
        if existing:
            raise ErpError(
                "PO_EXISTS", f"{po} already exists as order {existing['order_id']}. Update that order instead."
            )
        _validate(conn, req.customer_id, req.ship_to_address_id, req.lines)
        n = conn.execute("SELECT COUNT(*) FROM orders WHERE order_id > 'SO-1000'").fetchone()[
            0
        ]  # seed orders use lower ids
        order_id = f"SO-{1001 + n}"
        while conn.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone():
            n += 1
            order_id = f"SO-{1001 + n}"
        ts = now_iso()
        conn.execute(
            "INSERT INTO orders(order_id,customer_id,po_number,ship_to_address_id,version,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",
            (order_id, req.customer_id, po, req.ship_to_address_id, ts, ts),
        )
        _write_lines(conn, order_id, req.lines)
        changes = [f"Created with {len(req.lines)} line(s)"]
        conn.execute(
            "INSERT INTO order_history(order_id,version,action,changes,source_note,at) VALUES (?,1,'created',?,?,?)",
            (order_id, json.dumps(changes), req.source_note, ts),
        )
        conn.execute(
            "INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?)",
            (req.idempotency_key, order_id, 1, "created", rhash, ts),
        )
        order = get_order_by_id(conn, order_id)
    assert order is not None
    return OrderWriteResult(outcome="created", order=order, changes=changes)


def update_order(conn: sqlite3.Connection, req: UpdateOrderRequest) -> OrderWriteResult:
    rhash = _request_hash("update:" + req.model_dump_json(exclude={"source_note"}))
    with transaction(conn):
        replay = _replay(conn, req.idempotency_key, rhash)
        if replay:
            return replay
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (req.order_id,)).fetchone()
        if row is None:
            raise ErpError("ORDER_NOT_FOUND", f"No order {req.order_id!r}.")
        if row["version"] != req.expected_version:
            raise ErpError(
                "VERSION_CONFLICT",
                f"Order {req.order_id} is at version {row['version']}, but the change was prepared against version "
                f"{req.expected_version}. Someone changed it in the meantime; review the current order first.",
            )
        _validate(conn, row["customer_id"], req.ship_to_address_id, req.lines)
        before = get_order_by_id(conn, req.order_id)
        assert before is not None
        changes = _describe_changes(before, req)
        if not changes:
            raise ErpError("NO_CHANGE", "The update does not change anything.")
        version = row["version"] + 1
        ts = now_iso()
        _write_lines(conn, req.order_id, req.lines)
        conn.execute(
            "UPDATE orders SET ship_to_address_id=?, version=?, updated_at=? WHERE order_id=? AND version=?",
            (req.ship_to_address_id, version, ts, req.order_id, row["version"]),
        )
        conn.execute(
            "INSERT INTO order_history(order_id,version,action,changes,source_note,at) VALUES (?,?,'updated',?,?,?)",
            (req.order_id, version, json.dumps(changes), req.source_note, ts),
        )
        conn.execute(
            "INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?)",
            (req.idempotency_key, req.order_id, version, "updated", rhash, ts),
        )
        order = get_order_by_id(conn, req.order_id)
    assert order is not None
    return OrderWriteResult(outcome="updated", order=order, changes=changes)


def _describe_changes(before: ErpOrder, req: UpdateOrderRequest) -> list[str]:
    out = []
    if before.ship_to.address_id != req.ship_to_address_id:
        out.append(f"ship-to: {before.ship_to.address_id} -> {req.ship_to_address_id}")
    old = {ln.sku: ln for ln in before.lines}
    new = {ln.sku: ln for ln in req.lines}
    for sku, ln in new.items():
        if sku not in old:
            out.append(f"added {sku} {ln.quantity} {ln.unit}")
        elif (old[sku].quantity, old[sku].unit) != (ln.quantity, ln.unit):
            out.append(f"{sku}: {old[sku].quantity} {old[sku].unit} -> {ln.quantity} {ln.unit}")
    for sku, old_ln in old.items():
        if sku not in new:
            out.append(f"removed {sku} {old_ln.quantity} {old_ln.unit}")
    if not out and [ln.sku for ln in before.lines] != [ln.sku for ln in req.lines]:
        out.append("line order changed")
    return out


def open_erp(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ErpError(
            "ERP_NOT_INITIALIZED", "The Demo ERP database does not exist. Run `uv run inbox-to-order init-db` first."
        )
    return connect(db_path)
