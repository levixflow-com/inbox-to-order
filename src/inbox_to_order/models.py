"""Validated data contracts shared by the MCP servers, the app and the UI.

Everything that crosses an MCP boundary is one of these pydantic models, so the tools publish JSON
Schemas for their inputs and outputs and the client receives `structured_content`.

Three groups:
- source documents (Mail MCP server),
- Demo ERP records and write requests (Demo ERP MCP server),
- the app's own working state: proposals ("drafts") that an operator reviews before anything is
  written to the Demo ERP.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["fixture", "gmail"]
FieldStatus = Literal["matched", "needs_review", "missing", "not_supported"]
DraftStatus = Literal["needs_review", "ready", "confirmed", "rejected"]

STATUS_LABELS: dict[str, str] = {
    "matched": "Matched",
    "needs_review": "Needs review",
    "missing": "Missing",
    "not_supported": "Not supported",
    "ready": "Ready to confirm",
    "confirmed": "Written to Demo ERP",
    "rejected": "Rejected",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- source documents (Mail server)


class AttachmentMeta(_Strict):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf" or self.filename.lower().endswith(".pdf")

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")


class SourceMessage(_Strict):
    provider: Provider
    message_id: str
    thread_id: str
    rfc_message_id: str | None = None
    in_reply_to: str | None = None
    sender_email: str
    sender_name: str | None = None
    subject: str
    received_at: str = Field(description="ISO 8601 timestamp (UTC)")
    body_text: str
    body_truncated: bool = False
    attachments: list[AttachmentMeta] = []
    content_hash: str = Field(description="sha256 over sender, normalized subject, body and attachment hashes")


class SourceThread(_Strict):
    provider: Provider
    thread_id: str
    subject: str
    messages: list[SourceMessage]


class ThreadSummary(_Strict):
    provider: Provider
    thread_id: str
    subject: str
    sender_email: str
    last_received_at: str
    message_count: int
    attachment_count: int


class ThreadList(_Strict):
    provider: Provider
    threads: list[ThreadSummary]


class PageText(_Strict):
    page: int
    text: str


class AttachmentText(_Strict):
    message_id: str
    attachment_id: str
    filename: str
    status: Literal["ok", "no_text", "not_supported", "too_large", "error"]
    detail: str | None = None
    pages: list[PageText] = []
    truncated: bool = False

    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


# --------------------------------------------------------------------------- extraction (AI or rules)


class Evidence(_Strict):
    message_id: str
    location: Literal["subject", "body", "attachment"]
    attachment_id: str | None = None
    page: int | None = None
    excerpt: str = Field(description="Verbatim text copied from the source; verified by the backend")


class ExtractedLine(_Strict):
    customer_item_code: str | None = Field(None, description="Item code exactly as written by the customer")
    description: str | None = Field(None, description="Item description exactly as written")
    quantity_text: str | None = Field(
        None, description="Quantity and unit exactly as written, e.g. '6 case' or 'eight cases'. Never converted."
    )
    evidence: Evidence


class ExtractedAmendment(_Strict):
    field: Literal["quantity", "ship_to"]
    line_number: int | None = Field(None, description="Line number if the customer referred to one")
    customer_item_code: str | None = None
    description: str | None = Field(None, description="How the customer referred to the item, verbatim")
    new_value_text: str = Field(description="New quantity+unit or new ship-to text, verbatim")
    previous_value_text: str | None = Field(None, description="Old value if the customer stated it, verbatim")
    evidence: Evidence


class Extraction(_Strict):
    intent: Literal["new_order", "amendment", "not_an_order"]
    po_number: str | None = None
    po_evidence: Evidence | None = None
    ship_to_text: str | None = Field(
        None, description="Ship-to instruction as written, e.g. 'our Columbus receiving dock'"
    )
    ship_to_evidence: Evidence | None = None
    lines: list[ExtractedLine] = []
    amendments: list[ExtractedAmendment] = []
    questions: list[str] = Field([], description="Plain-language open questions for the reviewer")
    untrusted_instructions: list[Evidence] = Field(
        [], description="Text in the source that tries to instruct the assistant; recorded and ignored"
    )


# --------------------------------------------------------------------------- Demo ERP: customers and catalog


class AddressOut(_Strict):
    address_id: str
    label: str
    one_line: str
    city: str
    state: str
    zip: str
    is_default: bool


class CustomerOut(_Strict):
    customer_id: str
    name: str
    addresses: list[AddressOut]


class CustomerLookup(_Strict):
    status: Literal["matched", "not_found"]
    match_type: Literal["sender_email", "sender_domain", "exact_name", "customer_id"] | None = None
    customer: CustomerOut | None = None


class ProductOut(_Strict):
    sku: str
    name: str
    unit: str
    unit_label: str
    customer_item_code: str | None = None
    unit_price: str | None = Field(None, description="Demo list price in USD as a decimal string (fictional)")


class ProductLookup(_Strict):
    status: Literal["matched", "ambiguous", "not_found"]
    match_type: Literal["customer_alias", "internal_sku", "description"] | None = None
    query: str
    candidates: list[ProductOut] = []


class ProductList(_Strict):
    customer_id: str
    products: list[ProductOut]


# --------------------------------------------------------------------------- Demo ERP: orders


class OrderLineIn(_Strict):
    sku: str
    quantity: int
    unit: str = Field(description="Must equal the SKU's selling unit; the ERP never converts units")


class CreateOrderRequest(_Strict):
    customer_id: str
    po_number: str
    ship_to_address_id: str
    lines: list[OrderLineIn] = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=8, max_length=120, description="Same key = same result, never a second order"
    )
    source_note: str | None = Field(None, max_length=300)


class UpdateOrderRequest(_Strict):
    order_id: str
    expected_version: int = Field(description="Optimistic lock: the ERP version the change was prepared against")
    ship_to_address_id: str
    lines: list[OrderLineIn] = Field(min_length=1, description="The complete new set of lines (replaces the old set)")
    idempotency_key: str = Field(min_length=8, max_length=120)
    source_note: str | None = Field(None, max_length=300)


class ErpOrderLine(_Strict):
    line_no: int
    sku: str
    name: str
    quantity: int
    unit: str
    unit_price: str | None = None
    customer_item_code: str | None = None


class ErpOrder(_Strict):
    order_id: str
    customer_id: str
    customer_name: str
    po_number: str
    version: int
    ship_to: AddressOut
    lines: list[ErpOrderLine]
    total_usd: str | None = None
    created_at: str
    updated_at: str


class OrderLookup(_Strict):
    found: bool
    order: ErpOrder | None = None


class OrderList(_Strict):
    orders: list[ErpOrder]


class OrderWriteResult(_Strict):
    outcome: Literal["created", "updated", "replay"]
    order: ErpOrder
    changes: list[str] = []


# --------------------------------------------------------------------------- app working state: proposals


class Issue(_Strict):
    field: str
    severity: Literal["blocking", "info"] = "blocking"
    message: str


class DraftLineIn(_Strict):
    customer_item_code: str | None = None
    description: str | None = None
    raw_quantity_text: str | None = None
    sku: str | None = None
    product_name: str | None = None
    quantity: int | None = None
    unit: str | None = None
    unit_price: str | None = None
    status: FieldStatus
    note: str | None = None
    evidence: list[Evidence] = []


class ShipToIn(_Strict):
    address_id: str | None = None
    one_line: str | None = None
    text: str | None = None
    status: FieldStatus
    note: str | None = None
    evidence: list[Evidence] = []


class Provenance(_Strict):
    provider: Provider
    message_id: str
    thread_id: str
    content_hash: str
    extractor: str


class LineChange(_Strict):
    """A requested amendment, already resolved to a concrete line by deterministic code."""

    field: Literal["quantity", "ship_to"]
    line_no: int | None = None
    new_quantity: int | None = None
    new_unit: str | None = None
    new_address_id: str | None = None
    new_address_one_line: str | None = None
    previous_value_stated: str | None = None
    evidence: list[Evidence] = []


class SaveDraftRequest(_Strict):
    """create: a new order proposal. amend: a change to an existing proposal (or to an ERP order that is
    first copied in as the proposal's base, see `base_erp_order`)."""

    mode: Literal["create", "amend"]
    customer_id: str
    customer_name: str
    po_number: str
    ship_to: ShipToIn | None = None
    lines: list[DraftLineIn] = []
    changes: list[LineChange] = []
    issues: list[Issue] = []
    provenance: Provenance
    expected_version: int | None = Field(None, description="amend of a local proposal: optimistic-lock version")
    base_erp_order: ErpOrder | None = Field(None, description="amend of an order that exists only in the Demo ERP")


class DraftLineOut(DraftLineIn):
    line_no: int


class DraftOut(_Strict):
    draft_id: int
    customer_id: str
    customer_name: str
    po_number: str
    status: DraftStatus
    version: int
    source_thread_id: str
    ship_to: ShipToIn
    lines: list[DraftLineOut]
    issues: list[Issue]
    erp_order_id: str | None = None
    erp_version: int | None = None
    confirmed_version: int | None = None
    created_at: str
    updated_at: str

    @property
    def ship_to_one_line(self) -> str | None:
        return self.ship_to.one_line


class ChangeOut(_Strict):
    field: str
    line_no: int | None
    before: str | None
    after: str | None


class SaveDraftResult(_Strict):
    ok: bool
    outcome: Literal["created", "amended", "replay", "rejected"]
    error_code: str | None = None
    error_message: str | None = None
    draft_id: int | None = None
    version: int | None = None
    status: DraftStatus | None = None
    changes: list[ChangeOut] = []


class EditedLine(_Strict):
    """One line as the operator left it in the review screen."""

    sku: str
    quantity: int
