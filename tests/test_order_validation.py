"""Business rules: quantities/units, catalog lookups, the Demo ERP's own validation of writes,
operator edits, and the confirm step (the only path that writes to the Demo ERP)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from inbox_to_order.models import (
    CreateOrderRequest,
    DraftLineIn,
    EditedLine,
    Evidence,
    Issue,
    OrderLineIn,
    Provenance,
    SaveDraftRequest,
    ShipToIn,
    UpdateOrderRequest,
)
from inbox_to_order.review import ReviewError, confirm_and_write
from inbox_to_order.services import drafts, erp
from inbox_to_order.services.quantities import parse_quantity

from .conftest import InProcessToolbox

pytestmark = pytest.mark.anyio
EV = Evidence(message_id="m1", location="body", excerpt="test")


def prov(mid: str = "m1") -> Provenance:
    return Provenance(provider="fixture", message_id=mid, thread_id="t1", content_hash="h-" + mid, extractor="test")


def line(sku="HS-1102", qty=6, unit="case", status="matched", code="PR-TAPE-2") -> DraftLineIn:
    return DraftLineIn(customer_item_code=code, sku=sku, quantity=qty, unit=unit, status=status, evidence=[EV])


def create(conn, **kw):
    req = dict(
        mode="create",
        customer_id="PINE-RIDGE",
        customer_name="Pine Ridge Office Products LLC",
        po_number="PO-1001",
        ship_to=ShipToIn(address_id="PR-COLUMBUS", status="matched"),
        lines=[line()],
        provenance=prov(),
    )
    req.update(kw)
    return drafts.save_draft(conn, SaveDraftRequest(**req))


def order_req(**kw) -> CreateOrderRequest:
    req = dict(
        customer_id="PINE-RIDGE",
        po_number="PO-5001",
        ship_to_address_id="PR-COLUMBUS",
        lines=[OrderLineIn(sku="HS-1102", quantity=6, unit="case")],
        idempotency_key="test-key-0001",
    )
    req.update(kw)
    return CreateOrderRequest(**req)


# --------------------------------------------------------------------------- reading the customer's words


@pytest.mark.parametrize(
    ("text", "qty", "unit", "problem"),
    [
        ("eight cases", 8, "case", False),
        ("6 case", 6, "case", False),
        ("3 rolls", 3, "roll", False),
        ("twenty-four boxes", 24, "box", False),
        ("twelve", 12, None, True),  # unit missing -> review, never assumed
        ("two dozen", None, None, True),
        ("5 cases or 6 boxes", None, None, True),
        ("0 case", None, "case", True),
        ("", None, None, True),
    ],
)
def test_parse_quantity(text, qty, unit, problem):
    p = parse_quantity(text)
    assert (p.quantity, p.unit, p.problem is not None) == (qty, unit, problem)


def test_find_customer_uses_explicit_records_only(erp_conn):
    assert erp.find_customer(erp_conn, "purchasing@pineridge.example.com").match_type == "sender_email"
    assert erp.find_customer(erp_conn, "someone.else@pineridge.example.com").match_type == "sender_domain"
    assert erp.find_customer(erp_conn, "orders@unknown-buyer.example.net").status == "not_found"
    assert erp.find_customer(erp_conn, "Pine Ridge").status == "not_found"  # no fuzzy name guessing


def test_find_product_item_codes_ambiguity_and_isolation(erp_conn):
    r = erp.find_product(erp_conn, "PINE-RIDGE", "pr-tape-2")
    assert (r.status, r.match_type, r.candidates[0].sku) == ("matched", "customer_alias", "HS-1102")
    r = erp.find_product(erp_conn, "PINE-RIDGE", "blue labels")
    assert r.status == "ambiguous" and {c.sku for c in r.candidates} == {"HS-2210", "HS-2215"}
    assert erp.find_product(erp_conn, "PINE-RIDGE", "packing tape").candidates[0].sku == "HS-1102"
    # Another customer's item code is not visible to Pine Ridge.
    assert erp.find_product(erp_conn, "PINE-RIDGE", "LS-001").status == "not_found"
    assert erp.find_product(erp_conn, "PINE-RIDGE", "unicorn glitter").status == "not_found"
    with pytest.raises(erp.ErpError):
        erp.find_product(erp_conn, "NOPE", "tape")


def test_line_amount_uses_decimal_rounding():
    assert erp.line_amount("18.50", 3) == Decimal("55.50")
    assert erp.line_amount("0.335", 1) == Decimal("0.34")  # ROUND_HALF_UP
    assert erp.line_amount(None, 3) is None


# --------------------------------------------------------------------------- proposals (app state)


def test_valid_proposal_is_ready_to_confirm(conn):
    res = create(conn)
    assert res.ok and res.outcome == "created" and res.status == "ready" and res.version == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"lines": [line(qty=None)]}, "INCOMPLETE_LINE"),
        ({"ship_to": ShipToIn(text="somewhere", status="matched")}, "INVALID_SHIP_TO"),
    ],
)
def test_proposal_store_rejects_inconsistent_payloads(conn, kwargs, code):
    res = create(conn, **kwargs)
    assert not res.ok and res.error_code == code
    assert conn.execute("SELECT COUNT(*) FROM order_drafts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM processed_sources").fetchone()[0] == 0


def test_quantity_range_enforced_by_schema():
    with pytest.raises(ValueError):
        DraftLineIn.model_validate({"status": "matched", "quantity": "lots"})


def test_po_must_be_unique_per_customer(conn):
    assert create(conn).ok
    assert create(conn, provenance=prov("m2")).error_code == "DRAFT_EXISTS"


def test_unresolved_line_forces_needs_review(conn):
    res = create(conn, lines=[line(), DraftLineIn(description="blue labels", status="needs_review", evidence=[EV])])
    assert res.ok and res.status == "needs_review"


# --------------------------------------------------------------------------- Demo ERP validates every write itself


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"lines": [OrderLineIn(sku="HS-1102", quantity=6, unit="roll")]}, "UNIT_MISMATCH"),
        ({"lines": [OrderLineIn(sku="HS-9999", quantity=6, unit="case")]}, "UNKNOWN_SKU"),
        ({"lines": [OrderLineIn(sku="HS-1102", quantity=0, unit="case")]}, "INVALID_QUANTITY"),
        ({"lines": [OrderLineIn(sku="HS-1102", quantity=1, unit="case")] * 2}, "DUPLICATE_SKU"),
        ({"ship_to_address_id": "LS-CHICAGO"}, "ADDRESS_NOT_ALLOWED"),  # another customer's address
        ({"po_number": "PO 1001; DROP TABLE"}, "INVALID_PO"),
        ({"customer_id": "LAKESHORE", "po_number": "PO-48190", "ship_to_address_id": "LS-CHICAGO"}, "PO_EXISTS"),
        ({"customer_id": "NOPE"}, "UNKNOWN_CUSTOMER"),
    ],
)
def test_erp_rejects_invalid_orders(erp_conn, kwargs, code):
    before = erp_conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    with pytest.raises(erp.ErpError) as exc:
        erp.create_order(erp_conn, order_req(**kwargs))
    assert exc.value.code == code
    assert erp_conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before


def test_erp_create_is_idempotent_per_key(erp_conn):
    first = erp.create_order(erp_conn, order_req())
    assert first.outcome == "created" and first.order.order_id == "SO-1001" and first.order.version == 1
    again = erp.create_order(erp_conn, order_req(source_note="retry after a timeout"))
    assert again.outcome == "replay" and again.order.order_id == "SO-1001"
    with pytest.raises(erp.ErpError) as exc:  # same key, different content
        erp.create_order(erp_conn, order_req(lines=[OrderLineIn(sku="HS-1102", quantity=7, unit="case")]))
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert erp_conn.execute("SELECT COUNT(*) FROM orders WHERE po_number='PO-5001'").fetchone()[0] == 1


def test_erp_update_checks_version_and_records_history(erp_conn):
    def upd(version: int, qty: int, key: str) -> UpdateOrderRequest:
        return UpdateOrderRequest(
            order_id="SO-0950",
            expected_version=version,
            ship_to_address_id="LS-CHICAGO",
            lines=[
                OrderLineIn(sku="HS-1102", quantity=4, unit="case"),
                OrderLineIn(sku="HS-5120", quantity=qty, unit="roll"),
            ],
            idempotency_key=key,
        )

    res = erp.update_order(erp_conn, upd(1, 12, "update-key-1"))
    assert res.outcome == "updated" and res.order.version == 2 and res.changes == ["HS-5120: 10 roll -> 12 roll"]
    with pytest.raises(erp.ErpError) as exc:
        erp.update_order(erp_conn, upd(1, 14, "update-key-2"))  # prepared against the old version
    assert exc.value.code == "VERSION_CONFLICT"
    with pytest.raises(erp.ErpError) as exc:
        erp.update_order(erp_conn, upd(2, 12, "update-key-3"))
    assert exc.value.code == "NO_CHANGE"
    assert [r["version"] for r in erp.order_history(erp_conn, "SO-0950")] == [1, 2]


# --------------------------------------------------------------------------- operator: edit, reject, confirm


def _catalog(erp_conn):
    return {p.sku: p for p in erp.list_products(erp_conn, "PINE-RIDGE").products}


def _address(erp_conn, address_id: str):
    return next(a for a in erp.get_customer(erp_conn, "PINE-RIDGE").customer.addresses if a.address_id == address_id)


def test_operator_edit_resolves_questions_and_records_a_revision(conn, erp_conn):
    res = create(
        conn,
        lines=[
            DraftLineIn(description="Blue labels", raw_quantity_text="twelve", status="needs_review", evidence=[EV]),
            line(sku="HS-4407", qty=3, code="PR-MAIL-10"),
        ],
        issues=[Issue(field="lines", message="Which catalog item do you mean by 'Blue labels'?")],
    )
    assert res.status == "needs_review"
    d = drafts.operator_edit(
        conn,
        res.draft_id,
        1,
        ship_to=_address(erp_conn, "PR-DAYTON"),
        catalog=_catalog(erp_conn),
        lines=[EditedLine(sku="HS-2210", quantity=12), EditedLine(sku="HS-4407", quantity=3)],
        note="Customer confirmed by phone: blue shipping labels, boxes",
    )
    assert (d.status, d.version) == ("ready", 2)
    assert (d.lines[0].sku, d.lines[0].quantity, d.lines[0].unit) == ("HS-2210", 12, "box")
    assert d.lines[1].evidence == [EV]  # untouched line keeps its evidence
    assert d.ship_to.address_id == "PR-DAYTON"
    assert any(i.message.startswith("Resolved by the operator") for i in d.issues)
    rev = conn.execute(
        "SELECT kind, actor FROM draft_revisions WHERE draft_id=? AND version=2", (d.draft_id,)
    ).fetchone()
    assert tuple(rev) == ("edited", "operator")


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        ([EditedLine(sku="HS-1102", quantity=1), EditedLine(sku="HS-1102", quantity=2)], "DUPLICATE_SKU"),
        ([EditedLine(sku="HS-9999", quantity=1)], "UNKNOWN_SKU"),
        ([EditedLine(sku="HS-1102", quantity=0)], "INVALID_QUANTITY"),
        ([], "NO_LINES"),
    ],
)
def test_operator_edit_is_validated(conn, erp_conn, lines, code):
    res = create(conn)
    with pytest.raises(drafts.DraftError) as exc:
        drafts.operator_edit(
            conn, res.draft_id, 1, ship_to=_address(erp_conn, "PR-COLUMBUS"), catalog=_catalog(erp_conn), lines=lines
        )
    assert exc.value.code == code
    assert drafts.get_draft_by_id(conn, res.draft_id).version == 1


async def test_confirm_creates_then_updates_the_erp_order(conn, erp_conn):
    box = InProcessToolbox(erp_conn)
    res = create(conn)
    written = await confirm_and_write(conn, box, res.draft_id, 1)  # type: ignore[arg-type]
    assert written.outcome == "created" and written.order.po_number == "PO-1001"
    d = drafts.get_draft_by_id(conn, res.draft_id)
    assert (d.status, d.erp_order_id, d.erp_version, d.confirmed_version) == ("confirmed", written.order.order_id, 1, 1)

    d = drafts.operator_edit(
        conn,
        d.draft_id,
        1,
        ship_to=_address(erp_conn, "PR-COLUMBUS"),
        catalog=_catalog(erp_conn),
        lines=[EditedLine(sku="HS-1102", quantity=9)],
    )
    assert d.status == "ready" and d.erp_order_id == written.order.order_id
    updated = await confirm_and_write(conn, box, d.draft_id, 2)  # type: ignore[arg-type]
    assert (
        updated.outcome == "updated" and updated.order.version == 2 and updated.changes == ["HS-1102: 6 case -> 9 case"]
    )
    assert [name for name, _ in box.calls] == ["create_order", "update_order"]
    events = [
        r["event"] for r in conn.execute("SELECT event FROM audit_events WHERE actor='operator' ORDER BY event_id")
    ]
    assert events == ["confirmed_erp_created", "proposal_edited", "confirmed_erp_updated"]


async def test_confirm_refuses_unready_or_stale_proposals(conn, erp_conn):
    box = InProcessToolbox(erp_conn)
    blocked = create(conn, po_number="PO-2", lines=[DraftLineIn(description="x", status="needs_review", evidence=[EV])])
    with pytest.raises(ReviewError) as exc:
        await confirm_and_write(conn, box, blocked.draft_id, 1)  # type: ignore[arg-type]
    assert exc.value.code == "NOT_READY"
    ok = create(conn, po_number="PO-3", provenance=prov("m3"))
    with pytest.raises(ReviewError) as exc:
        await confirm_and_write(conn, box, ok.draft_id, 99)  # type: ignore[arg-type]
    assert exc.value.code == "VERSION_CONFLICT"
    assert box.calls == []  # nothing reached the ERP


async def test_confirm_reports_erp_refusal_and_keeps_the_proposal(conn, erp_conn):
    """The ERP is the last line of defence: here the PO was created in the ERP meanwhile by someone else."""
    erp.create_order(erp_conn, order_req(po_number="PO-1001", idempotency_key="someone-else-1"))
    res = create(conn)
    with pytest.raises(ReviewError) as exc:
        await confirm_and_write(conn, InProcessToolbox(erp_conn), res.draft_id, 1)  # type: ignore[arg-type]
    assert exc.value.code == "PO_EXISTS"
    assert drafts.get_draft_by_id(conn, res.draft_id).status == "ready"
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE event='erp_write_failed'").fetchone()[0] == 1


def test_reject(conn):
    res = create(conn)
    d = drafts.reject_draft(conn, res.draft_id, 1, "Customer cancelled")
    assert d.status == "rejected" and "Customer cancelled" in d.issues[-1].message
