"""Operator actions that need the Demo ERP: loading choices for the edit form, and Confirm.

`confirm_and_write` is the ONLY code path that calls the ERP write tools (create_order /
update_order). It runs when an operator clicks "Confirm" in the UI (or calls it explicitly from a
test). The language model is never offered these tools.
"""

from __future__ import annotations

import sqlite3

from . import db
from .mcp_client import Toolbox, ToolCallError
from .models import CreateOrderRequest, CustomerLookup, OrderLineIn, OrderWriteResult, ProductList, UpdateOrderRequest
from .services import drafts


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def load_choices(box: Toolbox, customer_id: str) -> tuple[ProductList, CustomerLookup]:
    """Catalog and ship-to addresses for the operator's edit form, read from the Demo ERP."""
    products = ProductList.model_validate(await box.call("list_products", {"customer_id": customer_id}))
    customer = CustomerLookup.model_validate(await box.call("get_customer", {"customer_id": customer_id}))
    return products, customer


async def confirm_and_write(
    conn: sqlite3.Connection, box: Toolbox, draft_id: int, expected_version: int
) -> OrderWriteResult:
    """Write the confirmed proposal to the Demo ERP: create the order, or update the existing one."""
    d = drafts.get_draft_by_id(conn, draft_id)
    if d is None:
        raise ReviewError("DRAFT_NOT_FOUND", f"No proposal {draft_id}.")
    if d.version != expected_version:
        raise ReviewError(
            "VERSION_CONFLICT", f"The proposal changed (now version {d.version}). Review it again before confirming."
        )
    if d.status != "ready":
        raise ReviewError("NOT_READY", "Only proposals without open questions ('Ready to confirm') can be confirmed.")
    if not d.ship_to.address_id or not d.lines or any(not (ln.sku and ln.quantity and ln.unit) for ln in d.lines):
        raise ReviewError(
            "INCOMPLETE", "Every line needs a catalog item, a quantity and a unit, and a ship-to must be chosen."
        )
    lines = [OrderLineIn(sku=ln.sku, quantity=ln.quantity, unit=ln.unit) for ln in d.lines]
    key = f"proposal-{d.draft_id}-v{d.version}"
    note = f"Confirmed by the operator in Inbox to Order, proposal {d.draft_id} version {d.version}"[:300]
    try:
        if d.erp_order_id is None:
            req = CreateOrderRequest(
                customer_id=d.customer_id,
                po_number=d.po_number,
                ship_to_address_id=d.ship_to.address_id,
                lines=lines,
                idempotency_key=key,
                source_note=note,
            )
            raw = await box.call("create_order", {"request": req.model_dump(mode="json")})
        else:
            assert d.erp_version is not None
            req2 = UpdateOrderRequest(
                order_id=d.erp_order_id,
                expected_version=d.erp_version,
                ship_to_address_id=d.ship_to.address_id,
                lines=lines,
                idempotency_key=key,
                source_note=note,
            )
            raw = await box.call("update_order", {"request": req2.model_dump(mode="json")})
    except ToolCallError as exc:
        db.audit(
            conn,
            "operator",
            "erp_write_failed",
            thread_id=d.source_thread_id,
            draft_id=d.draft_id,
            version=d.version,
            po_number=d.po_number,
            error_code=exc.code,
        )
        raise ReviewError(exc.code, exc.message) from exc
    result = OrderWriteResult.model_validate(raw)
    drafts.mark_confirmed(conn, d.draft_id, d.version, result.order.order_id, result.order.version)
    db.audit(
        conn,
        "operator",
        f"confirmed_erp_{result.outcome}",
        thread_id=d.source_thread_id,
        draft_id=d.draft_id,
        version=d.version,
        po_number=d.po_number,
        order_id=result.order.order_id,
        erp_version=result.order.version,
        changes=len(result.changes),
    )
    return result
