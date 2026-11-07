"""Order proposals ("drafts"): the app's working copy of an order before an operator confirms it.

A proposal is created or amended from an email (by the assistant + deterministic checks), can be
edited by the operator, and is written to the Demo ERP only when the operator confirms it
(see review.py). Nothing in this module talks to the ERP or to a model.
"""

from __future__ import annotations

import json
import sqlite3

from ..config import MAX_LINE_QUANTITY
from ..db import audit, now_iso, processed_record, transaction
from ..models import (
    AddressOut,
    ChangeOut,
    DraftLineIn,
    DraftLineOut,
    DraftOut,
    EditedLine,
    ErpOrder,
    Issue,
    ProductOut,
    SaveDraftRequest,
    SaveDraftResult,
    ShipToIn,
)


class DraftError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- read


def _draft_out(conn: sqlite3.Connection, row: sqlite3.Row) -> DraftOut:
    lines = [
        DraftLineOut(
            line_no=r["line_no"],
            customer_item_code=r["customer_item_code"],
            description=r["description"],
            raw_quantity_text=r["raw_quantity_text"],
            sku=r["sku"],
            product_name=r["product_name"],
            quantity=r["quantity"],
            unit=r["unit"],
            unit_price=r["unit_price"],
            status=r["status"],
            note=r["note"],
            evidence=json.loads(r["evidence"]),
        )
        for r in conn.execute(
            "SELECT * FROM draft_lines WHERE draft_id=? ORDER BY line_no", (row["draft_id"],)
        ).fetchall()
    ]
    return DraftOut(
        draft_id=row["draft_id"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        po_number=row["po_number"],
        status=row["status"],
        version=row["version"],
        source_thread_id=row["source_thread_id"],
        ship_to=ShipToIn.model_validate_json(row["ship_to"]),
        lines=lines,
        issues=[Issue.model_validate(i) for i in json.loads(row["issues"])],
        erp_order_id=row["erp_order_id"],
        erp_version=row["erp_version"],
        confirmed_version=row["confirmed_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_draft_by_id(conn: sqlite3.Connection, draft_id: int) -> DraftOut | None:
    row = conn.execute("SELECT * FROM order_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    return _draft_out(conn, row) if row else None


def find_draft(conn: sqlite3.Connection, customer_id: str, po_number: str) -> DraftOut | None:
    row = conn.execute(
        "SELECT * FROM order_drafts WHERE customer_id=? AND po_number=?", (customer_id, po_number.strip().upper())
    ).fetchone()
    return _draft_out(conn, row) if row else None


def drafts_for_thread(conn: sqlite3.Connection, provider: str, thread_id: str) -> list[DraftOut]:
    rows = conn.execute(
        "SELECT DISTINCT d.* FROM order_drafts d JOIN processed_sources p ON p.draft_id=d.draft_id "
        "WHERE p.provider=? AND p.thread_id=? ORDER BY d.draft_id",
        (provider, thread_id),
    ).fetchall()
    return [_draft_out(conn, r) for r in rows]


# --------------------------------------------------------------------------- write: from an email


def _reject(code: str, message: str, **kw) -> SaveDraftResult:  # noqa: ANN003
    return SaveDraftResult(ok=False, outcome="rejected", error_code=code, error_message=message, **kw)


def status_for(ship_to: ShipToIn, line_statuses: list[str], issues: list[Issue]) -> str:
    if (
        any(i.severity == "blocking" for i in issues)
        or ship_to.status != "matched"
        or not line_statuses
        or any(s != "matched" for s in line_statuses)
    ):
        return "needs_review"
    return "ready"


def _check_line(ln: DraftLineIn) -> None:
    if ln.quantity is not None and not (1 <= ln.quantity <= MAX_LINE_QUANTITY):
        raise DraftError("INVALID_QUANTITY", f"Quantity {ln.quantity} outside 1-{MAX_LINE_QUANTITY}.")
    if ln.status == "matched" and not (ln.sku and ln.quantity and ln.unit):
        raise DraftError("INCOMPLETE_LINE", "A line marked matched needs sku, quantity and unit.")


def save_draft(conn: sqlite3.Connection, req: SaveDraftRequest) -> SaveDraftResult:
    try:
        with transaction(conn):
            prior = processed_record(conn, req.provenance.provider, req.provenance.message_id)
            if prior is not None:
                return SaveDraftResult(
                    ok=True,
                    outcome="replay",
                    draft_id=prior["draft_id"],
                    version=prior["version"],
                    error_message="This source message was already processed; nothing changed.",
                )
            if req.mode == "create":
                return _create(conn, req)
            return _amend(conn, req)
    except DraftError as exc:
        return _reject(exc.code, exc.message)


def _insert_draft(
    conn: sqlite3.Connection,
    req: SaveDraftRequest,
    status: str,
    ship_to: ShipToIn,
    issues: list[Issue],
    lines: list[DraftLineIn],
    erp: ErpOrder | None = None,
) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO order_drafts(customer_id,customer_name,po_number,status,version,source_thread_id,ship_to,issues,"
        "erp_order_id,erp_version,confirmed_version,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)",
        (
            req.customer_id,
            req.customer_name,
            req.po_number.upper(),
            status,
            req.provenance.thread_id,
            ship_to.model_dump_json(),
            json.dumps([i.model_dump() for i in issues]),
            erp.order_id if erp else None,
            erp.version if erp else None,
            None,
            ts,
            ts,
        ),
    )
    draft_id = cur.lastrowid
    assert draft_id is not None
    _write_lines(conn, draft_id, lines)
    return draft_id


def _write_lines(conn: sqlite3.Connection, draft_id: int, lines: list[DraftLineIn]) -> None:
    conn.execute("DELETE FROM draft_lines WHERE draft_id=?", (draft_id,))
    for n, ln in enumerate(lines, start=1):
        conn.execute(
            "INSERT INTO draft_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                draft_id,
                n,
                ln.customer_item_code,
                ln.description,
                ln.raw_quantity_text,
                ln.sku,
                ln.product_name,
                ln.quantity,
                ln.unit,
                ln.unit_price,
                ln.status,
                ln.note,
                json.dumps([e.model_dump() for e in ln.evidence]),
            ),
        )


def _create(conn: sqlite3.Connection, req: SaveDraftRequest) -> SaveDraftResult:
    existing = find_draft(conn, req.customer_id, req.po_number)
    if existing:
        return _reject(
            "DRAFT_EXISTS",
            f"A proposal for {req.po_number} already exists; treat this message as a change or a duplicate.",
            draft_id=existing.draft_id,
            version=existing.version,
        )
    if req.ship_to is None:
        raise DraftError("INVALID_SHIP_TO", "ship_to is required when creating a proposal.")
    if req.ship_to.status == "matched" and not req.ship_to.address_id:
        raise DraftError("INVALID_SHIP_TO", "A matched ship-to must reference an address on file.")
    for ln in req.lines:
        _check_line(ln)
    issues = list(req.issues)
    if not req.lines:
        issues.append(Issue(field="lines", message="No order lines could be read from this message."))
    status = status_for(req.ship_to, [ln.status for ln in req.lines], issues)
    draft_id = _insert_draft(conn, req, status, req.ship_to, issues, req.lines)
    summary = [ChangeOut(field="proposal", line_no=None, before=None, after=f"created with {len(req.lines)} line(s)")]
    _finish(conn, req, draft_id, 1, "created", summary)
    return SaveDraftResult(ok=True, outcome="created", draft_id=draft_id, version=1, status=status, changes=summary)


def _copy_erp_order(conn: sqlite3.Connection, req: SaveDraftRequest, order: ErpOrder) -> int:
    """Create a proposal whose v1 is the order exactly as it is in the Demo ERP."""
    ship_to = ShipToIn(
        address_id=order.ship_to.address_id,
        one_line=order.ship_to.one_line,
        status="matched",
        note=f"From Demo ERP order {order.order_id}",
    )
    lines = [
        DraftLineIn(
            customer_item_code=ln.customer_item_code,
            sku=ln.sku,
            product_name=ln.name,
            quantity=ln.quantity,
            unit=ln.unit,
            unit_price=ln.unit_price,
            status="matched",
            note=f"From Demo ERP order {order.order_id}",
        )
        for ln in order.lines
    ]
    draft_id = _insert_draft(conn, req, "confirmed", ship_to, [], lines, erp=order)
    conn.execute("UPDATE order_drafts SET confirmed_version=1 WHERE draft_id=?", (draft_id,))
    conn.execute(
        "INSERT INTO draft_revisions(draft_id,version,kind,actor,source_message_id,changes,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            draft_id,
            1,
            "copied_from_erp",
            "demo-erp",
            None,
            json.dumps(
                [
                    ChangeOut(
                        field="proposal",
                        line_no=None,
                        before=None,
                        after=f"copied from Demo ERP order {order.order_id} v{order.version}",
                    ).model_dump()
                ]
            ),
            now_iso(),
        ),
    )
    return draft_id


def _amend(conn: sqlite3.Connection, req: SaveDraftRequest) -> SaveDraftResult:
    row = conn.execute(
        "SELECT * FROM order_drafts WHERE customer_id=? AND po_number=?", (req.customer_id, req.po_number.upper())
    ).fetchone()
    if req.base_erp_order is not None:
        if row is not None:
            return _reject("DRAFT_EXISTS", f"A proposal for {req.po_number} already exists; amend that one instead.")
        draft_id = _copy_erp_order(conn, req, req.base_erp_order)
        row = conn.execute("SELECT * FROM order_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    elif row is None:
        return _reject("DRAFT_NOT_FOUND", f"No existing proposal for {req.po_number}.")
    elif req.expected_version is None or req.expected_version != row["version"]:
        return _reject(
            "VERSION_CONFLICT",
            f"Proposal {req.po_number} is at version {row['version']}, but the change was prepared against {req.expected_version}. Reload and retry.",
            draft_id=row["draft_id"],
            version=row["version"],
        )
    draft_id = row["draft_id"]
    changes: list[ChangeOut] = []
    ship_to = ShipToIn.model_validate_json(row["ship_to"])
    for ch in req.changes:
        if ch.field == "quantity":
            line = conn.execute(
                "SELECT * FROM draft_lines WHERE draft_id=? AND line_no=?", (draft_id, ch.line_no)
            ).fetchone()
            if line is None:
                raise DraftError("LINE_NOT_FOUND", f"{req.po_number} has no line {ch.line_no}.")
            if not line["sku"]:
                raise DraftError(
                    "LINE_UNRESOLVED", f"Line {ch.line_no} has no catalog item yet; resolve it before changing it."
                )
            if ch.new_quantity is None or ch.new_unit is None or not (1 <= ch.new_quantity <= MAX_LINE_QUANTITY):
                raise DraftError("INVALID_CHANGE", "A quantity change needs a valid new quantity and unit.")
            if ch.new_unit != line["unit"]:
                raise DraftError(
                    "UNIT_MISMATCH", f"Line {ch.line_no} is in '{line['unit']}', not '{ch.new_unit}'. No conversion."
                )
            before, after = f"{line['quantity']} {line['unit']}", f"{ch.new_quantity} {ch.new_unit}"
            evidence = json.loads(line["evidence"]) + [e.model_dump() for e in ch.evidence]
            conn.execute(
                "UPDATE draft_lines SET quantity=?, evidence=?, note=? WHERE draft_id=? AND line_no=?",
                (
                    ch.new_quantity,
                    json.dumps(evidence),
                    f"Changed from {before} by message {req.provenance.message_id}",
                    draft_id,
                    ch.line_no,
                ),
            )
            changes.append(ChangeOut(field="quantity", line_no=ch.line_no, before=before, after=after))
        elif ch.field == "ship_to":
            if not ch.new_address_id:
                raise DraftError("INVALID_SHIP_TO", "A ship-to change needs an address on file.")
            new = ShipToIn(
                address_id=ch.new_address_id, one_line=ch.new_address_one_line, status="matched", evidence=ch.evidence
            )
            changes.append(
                ChangeOut(
                    field="ship_to",
                    line_no=None,
                    before=ship_to.one_line or ship_to.address_id,
                    after=ch.new_address_one_line or ch.new_address_id,
                )
            )
            ship_to = new
    issues = [Issue.model_validate(x) for x in json.loads(row["issues"])] + list(req.issues)
    statuses = [
        r["status"] for r in conn.execute("SELECT status FROM draft_lines WHERE draft_id=?", (draft_id,)).fetchall()
    ]
    status = status_for(ship_to, statuses, issues)  # a confirmed order always goes back to the operator
    version = row["version"] + 1
    conn.execute(
        "UPDATE order_drafts SET status=?, version=?, ship_to=?, issues=?, updated_at=? WHERE draft_id=? AND version=?",
        (
            status,
            version,
            ship_to.model_dump_json(),
            json.dumps([i.model_dump() for i in issues]),
            now_iso(),
            draft_id,
            row["version"],
        ),
    )
    _finish(conn, req, draft_id, version, "amended", changes)
    return SaveDraftResult(
        ok=True, outcome="amended", draft_id=draft_id, version=version, status=status, changes=changes
    )


def _finish(
    conn: sqlite3.Connection, req: SaveDraftRequest, draft_id: int, version: int, kind: str, changes: list[ChangeOut]
) -> None:
    p = req.provenance
    conn.execute(
        "INSERT INTO draft_revisions(draft_id,version,kind,actor,source_message_id,changes,created_at) VALUES (?,?,?,?,?,?,?)",
        (draft_id, version, kind, "assistant", p.message_id, json.dumps([c.model_dump() for c in changes]), now_iso()),
    )
    conn.execute(
        "INSERT INTO processed_sources(provider,message_id,thread_id,content_hash,outcome,draft_id,version,detail,processed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            p.provider,
            p.message_id,
            p.thread_id,
            p.content_hash,
            kind,
            draft_id,
            version,
            json.dumps({"extractor": p.extractor}),
            now_iso(),
        ),
    )
    audit(
        conn,
        "app",
        f"proposal_{kind}",
        thread_id=p.thread_id,
        message_id=p.message_id,
        draft_id=draft_id,
        version=version,
        po_number=req.po_number,
        extractor=p.extractor,
        changes=len(changes),
    )


# --------------------------------------------------------------------------- write: operator decisions (not MCP tools)


def _load_for_operator(conn: sqlite3.Connection, draft_id: int, expected_version: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM order_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        raise DraftError("DRAFT_NOT_FOUND", f"No proposal {draft_id}.")
    if row["version"] != expected_version:
        raise DraftError("VERSION_CONFLICT", f"The proposal changed (now version {row['version']}). Review it again.")
    return row


def _describe(sku: str | None, qty: int | None, unit: str | None) -> str:
    return f"{sku or '?'} {qty if qty is not None else '?'} {unit or '?'}"


def operator_edit(
    conn: sqlite3.Connection,
    draft_id: int,
    expected_version: int,
    *,
    ship_to: AddressOut,
    lines: list[EditedLine],
    catalog: dict[str, ProductOut],
    note: str = "",
) -> DraftOut:
    """Save the operator's corrections as a new revision. Saving also marks the open questions as resolved
    by the operator; the result is 'ready' to confirm when every line is a valid catalog item."""
    with transaction(conn):
        row = _load_for_operator(conn, draft_id, expected_version)
        if row["status"] == "rejected":
            raise DraftError("REJECTED", "This proposal was rejected.")
        if not lines:
            raise DraftError("NO_LINES", "Keep at least one line, or reject the proposal instead.")
        old = _draft_out(conn, row)
        skus = [ln.sku for ln in lines]
        if len(set(skus)) != len(skus):
            raise DraftError("DUPLICATE_SKU", "The same item appears on two lines; combine them into one line.")
        new_lines: list[DraftLineIn] = []
        changes: list[ChangeOut] = []
        for n, ed in enumerate(lines, start=1):
            prod = catalog.get(ed.sku)
            if prod is None:
                raise DraftError("UNKNOWN_SKU", f"{ed.sku!r} is not a catalog item.")
            if not (1 <= ed.quantity <= MAX_LINE_QUANTITY):
                raise DraftError("INVALID_QUANTITY", f"Line {n}: quantity must be between 1 and {MAX_LINE_QUANTITY}.")
            prev = old.lines[n - 1] if n <= len(old.lines) else None
            same = prev is not None and prev.sku == ed.sku and prev.quantity == ed.quantity and prev.unit == prod.unit
            if prev is not None and prev.sku == ed.sku:
                base = prev.model_dump(exclude={"line_no"})
            else:
                base = {"customer_item_code": prod.customer_item_code, "evidence": []}
            base.update(
                sku=prod.sku,
                product_name=prod.name,
                unit=prod.unit,
                unit_price=prod.unit_price,
                quantity=ed.quantity,
                status="matched",
                note=prev.note if same and prev else "Set by the operator",
            )
            new_lines.append(DraftLineIn.model_validate(base))
            if not same:
                changes.append(
                    ChangeOut(
                        field="line",
                        line_no=n,
                        before=_describe(prev.sku, prev.quantity, prev.unit) if prev else None,
                        after=_describe(prod.sku, ed.quantity, prod.unit),
                    )
                )
        for prev in old.lines[len(lines) :]:
            changes.append(
                ChangeOut(
                    field="line", line_no=prev.line_no, before=_describe(prev.sku, prev.quantity, prev.unit), after=None
                )
            )
        new_ship = old.ship_to
        if ship_to.address_id != old.ship_to.address_id or old.ship_to.status != "matched":
            if ship_to.address_id != old.ship_to.address_id:
                changes.append(
                    ChangeOut(field="ship_to", line_no=None, before=old.ship_to.one_line, after=ship_to.one_line)
                )
            new_ship = ShipToIn(
                address_id=ship_to.address_id,
                one_line=ship_to.one_line,
                text=old.ship_to.text,
                status="matched",
                evidence=old.ship_to.evidence,
                note="Confirmed by the operator",
            )
        resolved = [
            Issue(field=i.field, severity="info", message=f"Resolved by the operator: {i.message}")
            for i in old.issues
            if i.severity == "blocking"
        ]
        kept = [i for i in old.issues if i.severity == "info"]
        if note.strip():
            kept.append(Issue(field="operator", severity="info", message=f"Operator note: {note.strip()[:300]}"))
        if not changes:
            changes.append(ChangeOut(field="review", line_no=None, before=None, after="reviewed, no value changed"))
        issues = kept + resolved
        status = status_for(new_ship, [ln.status for ln in new_lines], issues)
        version = row["version"] + 1
        _write_lines(conn, draft_id, new_lines)
        conn.execute(
            "UPDATE order_drafts SET status=?, version=?, ship_to=?, issues=?, updated_at=? WHERE draft_id=? AND version=?",
            (
                status,
                version,
                new_ship.model_dump_json(),
                json.dumps([i.model_dump() for i in issues]),
                now_iso(),
                draft_id,
                row["version"],
            ),
        )
        conn.execute(
            "INSERT INTO draft_revisions(draft_id,version,kind,actor,source_message_id,changes,created_at) VALUES (?,?,?,?,?,?,?)",
            (draft_id, version, "edited", "operator", None, json.dumps([c.model_dump() for c in changes]), now_iso()),
        )
        audit(
            conn,
            "operator",
            "proposal_edited",
            thread_id=row["source_thread_id"],
            draft_id=draft_id,
            version=version,
            po_number=row["po_number"],
            changes=len(changes),
        )
    out = get_draft_by_id(conn, draft_id)
    assert out is not None
    return out


def reject_draft(conn: sqlite3.Connection, draft_id: int, expected_version: int, note: str = "") -> DraftOut:
    with transaction(conn):
        row = _load_for_operator(conn, draft_id, expected_version)
        if row["status"] == "confirmed":
            raise DraftError("ALREADY_CONFIRMED", "This version is already in the Demo ERP; nothing to reject.")
        issues = json.loads(row["issues"]) + [
            Issue(
                field="operator", severity="info", message=f"Rejected by the operator. {note.strip()[:300]}".strip()
            ).model_dump()
        ]
        conn.execute(
            "UPDATE order_drafts SET status='rejected', issues=?, updated_at=? WHERE draft_id=?",
            (json.dumps(issues), now_iso(), draft_id),
        )
        audit(
            conn,
            "operator",
            "proposal_rejected",
            thread_id=row["source_thread_id"],
            draft_id=draft_id,
            version=row["version"],
            po_number=row["po_number"],
        )
    out = get_draft_by_id(conn, draft_id)
    assert out is not None
    return out


def mark_confirmed(conn: sqlite3.Connection, draft_id: int, version: int, order_id: str, erp_version: int) -> DraftOut:
    with transaction(conn):
        conn.execute(
            "UPDATE order_drafts SET status='confirmed', confirmed_version=?, erp_order_id=?, erp_version=?, updated_at=? "
            "WHERE draft_id=? AND version=?",
            (version, order_id, erp_version, now_iso(), draft_id, version),
        )
    out = get_draft_by_id(conn, draft_id)
    assert out is not None
    return out
