"""Deterministic validation between an extraction (AI or rules) and the proposal the operator reviews.

The extractor only proposes. This module decides what goes into the proposal:
- every evidence excerpt must literally appear in the cited source (anti-hallucination check);
- customer identity comes from the sender address via the Demo ERP records, never from the extractor;
- PO numbers must appear in the source text; several different POs -> review;
- SKUs come only from find_product (alias, SKU or unambiguous description), never from the model;
- quantities/units are parsed by code; a unit that differs from the selling unit is never converted;
- amendments must link to exactly one existing order: a proposal in this app, or else an order that
  already exists in the Demo ERP (customer + PO, cross-checked with the conversation).
Nothing here writes to the Demo ERP; that happens only after an operator confirms (review.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .mcp_client import Toolbox
from .models import (
    AddressOut,
    AttachmentText,
    CustomerLookup,
    DraftLineIn,
    DraftLineOut,
    DraftOut,
    ErpOrder,
    Evidence,
    ExtractedAmendment,
    ExtractedLine,
    Extraction,
    Issue,
    LineChange,
    ProductLookup,
    Provenance,
    SaveDraftRequest,
    ShipToIn,
    SourceMessage,
    SourceThread,
)
from .services.injection import find_instruction_like
from .services.quantities import parse_quantity
from .services.text import find_po_numbers, norm, normalize_po, tokens


@dataclass
class MessageContext:
    thread: SourceThread
    message: SourceMessage
    attachments: list[AttachmentText]
    customer: CustomerLookup
    thread_drafts: list[DraftOut] = field(default_factory=list)
    find_local_draft: Callable[[str, str], DraftOut | None] = lambda _cid, _po: None  # noqa: E731

    @property
    def customer_id(self) -> str | None:
        return self.customer.customer.customer_id if self.customer.customer else None

    def all_text(self) -> str:
        parts = [self.message.subject, self.message.body_text] + [a.full_text() for a in self.attachments]
        return "\n".join(parts)

    def source_for(self, ev: Evidence) -> str | None:
        if ev.message_id != self.message.message_id:
            return None
        if ev.location == "subject":
            return self.message.subject
        if ev.location == "body":
            return self.message.body_text
        for att in self.attachments:
            if att.attachment_id == ev.attachment_id:
                if ev.page is not None:
                    return next((p.text for p in att.pages if p.page == ev.page), None)
                return att.full_text()
        return None


def verify_evidence(ctx: MessageContext, ev: Evidence | None) -> bool:
    """True only if the excerpt is non-trivial and literally present in the cited source."""
    if ev is None or len(ev.excerpt.strip()) < 3:
        return False
    src = ctx.source_for(ev)
    return src is not None and norm(ev.excerpt) in norm(src)


@dataclass
class Plan:
    request: SaveDraftRequest | None
    outcome_without_request: str = "needs_review"  # needs_review | no_action
    issues: list[Issue] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)


def resolve_addresses(addresses: list[AddressOut], text: str) -> list[AddressOut]:
    t = text.lower()
    return [a for a in addresses if re.search(rf"\b{re.escape(a.city.lower())}\b", t) or re.search(rf"\b{a.zip}\b", t)]


async def build_plan(ctx: MessageContext, ex: Extraction, box: Toolbox, prov: Provenance) -> Plan:
    plan = Plan(request=None)
    _collect_untrusted(ctx, ex, plan)
    for q in ex.questions[:5]:
        plan.issues.append(Issue(field="question", severity="info", message=q[:300]))
    if ex.intent == "not_an_order":
        plan.outcome_without_request = "no_action"
        plan.notices.append("The message does not look like an order or an order change.")
        return plan
    if ctx.customer.status != "matched":
        plan.issues.append(
            Issue(
                field="customer",
                message=f"Sender {ctx.message.sender_email} is not a known customer in the Demo ERP. No customer was guessed.",
            )
        )
    po = _resolve_po(ctx, ex, plan)
    if ctx.customer_id is None or po is None:
        return plan
    if ex.intent == "amendment":
        return await _plan_amendment(ctx, ex, box, prov, po, plan)
    return await _plan_new_order(ctx, ex, box, prov, po, plan)


def _collect_untrusted(ctx: MessageContext, ex: Extraction, plan: Plan) -> None:
    seen: set[str] = set()
    for line in find_instruction_like(ctx.all_text()):
        seen.add(norm(line))
        plan.untrusted.append(line)
    for ev in ex.untrusted_instructions:
        if verify_evidence(ctx, ev) and norm(ev.excerpt) not in seen and not any(norm(ev.excerpt) in s for s in seen):
            plan.untrusted.append(ev.excerpt.strip()[:300])
    if plan.untrusted:
        plan.issues.append(
            Issue(
                field="source_content",
                severity="info",
                message=f"{len(plan.untrusted)} instruction-like passage(s) in the customer content were treated as data and ignored.",
            )
        )


def _resolve_po(ctx: MessageContext, ex: Extraction, plan: Plan) -> str | None:
    found = list(dict.fromkeys(po for po, _ in find_po_numbers(ctx.all_text())))
    if len(found) > 1:
        plan.issues.append(
            Issue(
                field="po_number",
                message=f"The message mentions several PO numbers ({', '.join(found)}). Which one applies?",
            )
        )
        return None
    if ex.po_number:
        po = normalize_po(ex.po_number)
        if po not in found:
            plan.issues.append(
                Issue(field="po_number", message=f"Proposed PO {ex.po_number!r} does not appear in the message text.")
            )
            return None
        return po
    if len(found) == 1:
        plan.notices.append(f"PO {found[0]} read from the message text by the deterministic scanner.")
        return found[0]
    plan.issues.append(
        Issue(field="po_number", message="No PO number found. A PO number is required to prepare an order or a change.")
    )
    return None


async def _lookup(box: Toolbox, customer_id: str, query: str) -> ProductLookup:
    return ProductLookup.model_validate(
        await box.call("find_product", {"customer_id": customer_id, "customer_code_or_description": query})
    )


def _candidates_text(lookup: ProductLookup) -> str:
    return "; ".join(f"{c.sku} {c.name} (per {c.unit})" for c in lookup.candidates[:5])


async def _resolve_ship_to(ctx: MessageContext, ex: Extraction) -> tuple[ShipToIn, list[Issue]]:
    issues: list[Issue] = []
    addresses = ctx.customer.customer.addresses if ctx.customer.customer else []
    pdf_ship = []
    for att in ctx.attachments:
        for m in re.finditer(r"^Ship\s*To:\s*(.+)$", att.full_text(), re.IGNORECASE | re.MULTILINE):
            pdf_ship.append(
                (
                    m.group(1).strip(),
                    Evidence(
                        message_id=ctx.message.message_id,
                        location="attachment",
                        attachment_id=att.attachment_id,
                        excerpt=m.group(0).strip(),
                    ),
                )
            )
    if ex.ship_to_text:
        ev = ex.ship_to_evidence
        if not verify_evidence(ctx, ev):
            return ShipToIn(
                text=ex.ship_to_text,
                status="needs_review",
                note="Ship-to instruction could not be verified against the source text.",
            ), issues
        hits = resolve_addresses(addresses, ex.ship_to_text)
        if len(hits) != 1:
            return ShipToIn(
                text=ex.ship_to_text,
                status="needs_review",
                evidence=[ev],
                note=f"'{ex.ship_to_text}' does not match exactly one address on file for this customer.",
            ), issues
        chosen = hits[0]
        for text, pev in pdf_ship:
            other = resolve_addresses(addresses, text)
            if len(other) != 1 or other[0].address_id != chosen.address_id:
                issues.append(
                    Issue(
                        field="ship_to",
                        message=f"Ship-to in the PDF ('{text}') disagrees with the instruction '{ex.ship_to_text}'.",
                    )
                )
                return ShipToIn(
                    address_id=chosen.address_id,
                    one_line=chosen.one_line,
                    text=ex.ship_to_text,
                    status="needs_review",
                    evidence=[ev, pev],
                ), issues
        return ShipToIn(
            address_id=chosen.address_id,
            one_line=chosen.one_line,
            text=ex.ship_to_text,
            status="matched",
            evidence=[ev] + [p for _, p in pdf_ship],
        ), issues
    if pdf_ship:
        text, pev = pdf_ship[0]
        hits = resolve_addresses(addresses, text)
        if len(hits) == 1:
            return ShipToIn(
                address_id=hits[0].address_id, one_line=hits[0].one_line, text=text, status="matched", evidence=[pev]
            ), issues
        return ShipToIn(
            text=text, status="needs_review", evidence=[pev], note="PDF ship-to does not match an address on file."
        ), issues
    default = next((a for a in addresses if a.is_default), None)
    return ShipToIn(
        address_id=default.address_id if default else None,
        one_line=default.one_line if default else None,
        status="missing",
        note="No ship-to was stated. The customer's default address is suggested; please confirm."
        if default
        else "No ship-to was stated.",
    ), issues


async def _resolve_line(ctx: MessageContext, ln: ExtractedLine, box: Toolbox) -> tuple[DraftLineIn, str | None]:
    cid = ctx.customer_id
    assert cid is not None
    notes: list[str] = []
    question = None
    status = "matched"
    product = None
    ref = ln.customer_item_code or ln.description
    if not verify_evidence(ctx, ln.evidence):
        status = "needs_review"
        notes.append("Evidence excerpt not found in the source; value not trusted.")
    if ln.customer_item_code:
        lookup = await _lookup(box, cid, ln.customer_item_code)
        if lookup.status == "matched" and lookup.match_type in ("customer_alias", "internal_sku"):
            product = lookup.candidates[0]
            words = {t for t in tokens(ln.description or "") if not t[0].isdigit()}
            if words and not words & (tokens(product.name)):
                status = "needs_review"
                notes.append(f"Description '{ln.description}' does not match {product.sku} {product.name}.")
        else:
            status = "needs_review"
            notes.append(
                f"Customer item code '{ln.customer_item_code}' is not in this customer's alias table. No substitute was chosen."
            )
    elif ln.description:
        lookup = await _lookup(box, cid, ln.description)
        if lookup.status == "matched":
            product = lookup.candidates[0]
            notes.append(f"Matched by description to {product.sku}.")
        elif lookup.status == "ambiguous":
            status = "needs_review"
            notes.append(f"Several catalog items match '{ln.description}': {_candidates_text(lookup)}.")
            question = f"Which catalog item do you mean by '{ln.description}'"
        else:
            status = "needs_review"
            notes.append(f"No catalog item matches '{ln.description}'.")
    else:
        status = "missing"
        notes.append("No item code or description.")

    qp = parse_quantity(ln.quantity_text)
    quantity, unit = qp.quantity, qp.unit
    unit_missing = qp.quantity is not None and qp.unit is None
    if qp.problem:
        if status == "matched":
            status = "missing" if not ln.quantity_text else "needs_review"
        notes.append(qp.problem + (" No unit was assumed." if unit_missing else ""))
    if unit_missing:
        question = (
            f"{question}, and in which selling unit" if question else f"In which selling unit should '{ref}' be ordered"
        )
    if product and unit and unit != product.unit:
        status = "needs_review"
        notes.append(
            f"Customer wrote '{ln.quantity_text}', but {product.sku} sells by {product.unit_label}. No conversion was made."
        )
        unit = None
    line = DraftLineIn(
        customer_item_code=ln.customer_item_code,
        description=ln.description,
        raw_quantity_text=ln.quantity_text,
        sku=product.sku if product else None,
        product_name=product.name if product else None,
        unit_price=product.unit_price if product else None,
        quantity=quantity,
        unit=unit,
        status=status,
        note=" ".join(notes) or None,
        evidence=[ln.evidence],
    )
    return line, (question + "?") if question else None


async def _plan_new_order(
    ctx: MessageContext, ex: Extraction, box: Toolbox, prov: Provenance, po: str, plan: Plan
) -> Plan:
    cid = ctx.customer_id
    assert cid is not None
    local = ctx.find_local_draft(cid, po)
    if local is not None:
        plan.issues.append(
            Issue(
                field="po_number",
                message=f"A proposal for {po} already exists (version {local.version}). This message was not applied; "
                "check whether it is a revised PO or a duplicate.",
            )
        )
        return plan
    existing = await box.call("get_order", {"customer_id": cid, "po_number": po})
    if existing["found"]:
        plan.issues.append(
            Issue(
                field="po_number",
                message=f"{po} already exists in the Demo ERP as order {existing['order']['order_id']}. This message was not "
                "applied as a new order; check whether it is a change or a duplicate.",
            )
        )
        return plan
    ship_to, ship_issues = await _resolve_ship_to(ctx, ex)
    issues = list(plan.issues) + ship_issues
    lines = []
    for ln in ex.lines:
        line, question = await _resolve_line(ctx, ln, box)
        lines.append(line)
        if question:
            issues.append(Issue(field="lines", message=question))
    for att in ctx.attachments:
        if att.status != "ok":
            issues.append(
                Issue(field=f"attachment:{att.filename}", message=att.detail or f"Attachment status: {att.status}")
            )
    plan.issues = issues
    plan.request = SaveDraftRequest(
        mode="create",
        customer_id=cid,
        customer_name=_customer_name(ctx),
        po_number=po,
        ship_to=ship_to,
        lines=lines,
        issues=issues,
        provenance=prov,
    )
    return plan


async def _plan_amendment(
    ctx: MessageContext, ex: Extraction, box: Toolbox, prov: Provenance, po: str, plan: Plan
) -> Plan:
    cid = ctx.customer_id
    assert cid is not None
    thread_pos = {d.po_number for d in ctx.thread_drafts if d.customer_id == cid}
    if thread_pos and po not in thread_pos:
        plan.issues.append(
            Issue(
                field="po_number",
                message=f"The message refers to {po}, but this conversation is linked to {', '.join(sorted(thread_pos))}. Resolve manually.",
            )
        )
        return plan
    draft = ctx.find_local_draft(cid, po)
    base_erp: ErpOrder | None = None
    if draft is None:
        lookup = await box.call("get_order", {"customer_id": cid, "po_number": po})
        if not lookup["found"]:
            plan.issues.append(
                Issue(
                    field="po_number",
                    message=f"This looks like a change to {po}, but no such order exists "
                    "in this app or in the Demo ERP.",
                )
            )
            return plan
        base_erp = ErpOrder.model_validate(lookup["order"])
        draft = _draft_view_of_erp_order(base_erp)
        plan.notices.append(
            f"{po} exists in the Demo ERP as order {base_erp.order_id} (version {base_erp.version}); "
            "the change is prepared against that order."
        )
    elif not thread_pos:
        plan.notices.append(f"Linked to {po} by customer and PO number (the message is in a different conversation).")
    if draft.status == "confirmed" and base_erp is None:
        plan.notices.append(
            f"{po} is already in the Demo ERP (proposal version {draft.version}); this change needs a new "
            "confirmation before the ERP order is updated."
        )

    changes: list[LineChange] = []
    issues = list(plan.issues)
    for am in ex.amendments:
        change, issue = await _resolve_amendment(ctx, am, draft, box)
        if change:
            changes.append(change)
        if issue:
            issues.append(issue)
    if ex.lines:
        issues.append(
            Issue(
                field="lines",
                message="The message seems to add new items. Adding lines from a change email is "
                "not automated in this proof of concept; the operator can add them in the review screen.",
            )
        )
    if not changes and not [i for i in issues if i.severity == "blocking"]:
        plan.outcome_without_request = "no_action"
        plan.notices.append("No concrete change was found to apply.")
        plan.issues = issues
        return plan
    plan.issues = issues
    plan.request = SaveDraftRequest(
        mode="amend",
        customer_id=cid,
        customer_name=_customer_name(ctx),
        po_number=po,
        changes=changes,
        issues=issues,
        provenance=prov,
        expected_version=None if base_erp else draft.version,
        base_erp_order=base_erp,
    )
    return plan


def _customer_name(ctx: MessageContext) -> str:
    return ctx.customer.customer.name if ctx.customer.customer else "?"


def _draft_view_of_erp_order(order: ErpOrder) -> DraftOut:
    """Present an ERP order in the same shape as a proposal, so one amendment resolver serves both."""
    return DraftOut(
        draft_id=0,
        customer_id=order.customer_id,
        customer_name=order.customer_name,
        po_number=order.po_number,
        status="confirmed",
        version=0,
        source_thread_id="",
        erp_order_id=order.order_id,
        erp_version=order.version,
        ship_to=ShipToIn(address_id=order.ship_to.address_id, one_line=order.ship_to.one_line, status="matched"),
        lines=[
            DraftLineOut(
                line_no=ln.line_no,
                customer_item_code=ln.customer_item_code,
                sku=ln.sku,
                product_name=ln.name,
                quantity=ln.quantity,
                unit=ln.unit,
                unit_price=ln.unit_price,
                status="matched",
            )
            for ln in order.lines
        ],
        issues=[],
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


async def _resolve_amendment(
    ctx: MessageContext, am: ExtractedAmendment, draft: DraftOut, box: Toolbox
) -> tuple[LineChange | None, Issue | None]:
    if not verify_evidence(ctx, am.evidence):
        return None, Issue(
            field="amendment",
            message=f"A proposed change ('{am.new_value_text}') could not be verified against the message text.",
        )
    if am.field == "ship_to":
        addresses = ctx.customer.customer.addresses if ctx.customer.customer else []
        hits = resolve_addresses(addresses, am.new_value_text)
        if len(hits) != 1:
            return None, Issue(
                field="ship_to",
                message=f"New ship-to '{am.new_value_text}' does not match exactly one address on file.",
            )
        if hits[0].address_id == draft.ship_to.address_id:
            return None, None
        return LineChange(
            field="ship_to",
            new_address_id=hits[0].address_id,
            new_address_one_line=hits[0].one_line,
            evidence=[am.evidence],
        ), None

    ref = am.customer_item_code or am.description or (f"line {am.line_number}" if am.line_number else "?")
    if am.line_number is not None:
        targets = [ln for ln in draft.lines if ln.line_no == am.line_number]
    elif am.customer_item_code:
        targets = [ln for ln in draft.lines if (ln.customer_item_code or "").upper() == am.customer_item_code.upper()]
    elif am.description:
        lookup = await _lookup(box, draft.customer_id, am.description)
        skus = {c.sku for c in lookup.candidates}
        targets = [ln for ln in draft.lines if ln.sku in skus]
    else:
        targets = []
    if len(targets) != 1:
        return None, Issue(
            field="amendment",
            message=f"Could not identify exactly one line of {draft.po_number} for '{ref}'. Which line is meant?",
        )
    line = targets[0]
    new = parse_quantity(am.new_value_text)
    if new.problem:
        return None, Issue(
            field=f"line {line.line_no}",
            message=f"Change for '{ref}': {new.problem} Which quantity and selling unit are meant?",
        )
    if new.unit != line.unit:
        return None, Issue(
            field=f"line {line.line_no}",
            message=f"Change for '{ref}' is in '{new.unit}', but line {line.line_no} is in '{line.unit}'. No conversion was made.",
        )
    if am.previous_value_text:
        prev = parse_quantity(am.previous_value_text)
        if prev.quantity is not None and prev.quantity != line.quantity:
            return None, Issue(
                field=f"line {line.line_no}",
                message=f"The customer says '{ref}' was {prev.quantity}, but the order has {line.quantity} {line.unit}. Not applied.",
            )
    if new.quantity == line.quantity:
        return None, None
    return LineChange(
        field="quantity",
        line_no=line.line_no,
        new_quantity=new.quantity,
        new_unit=new.unit,
        previous_value_stated=am.previous_value_text,
        evidence=[am.evidence],
    ), None
