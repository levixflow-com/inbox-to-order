"""Extractors turn one source message into a proposed `Extraction`.

- `RuleBasedExtractor` is a deliberately small, deterministic baseline. It reads the structured
  PDF table used by the demo fixtures and a few common phrasings. It exists so the whole pipeline
  (MCP, validation, drafts, review UI) runs without an API key, and so tests are deterministic.
  It is NOT AI and is labeled as such everywhere in the UI.
- `ClaudeExtractor` (agent.py) is the AI path: Claude reads the thread through MCP tools.

Whatever an extractor returns is only a proposal; pipeline.py validates it.
"""

from __future__ import annotations

import re
from typing import Protocol

from .mcp_client import Toolbox
from .models import Evidence, ExtractedAmendment, ExtractedLine, Extraction
from .pipeline import MessageContext
from .services.text import find_po_numbers


class ExtractionError(RuntimeError):
    """The extractor could not produce a valid proposal. Nothing is persisted."""


class Extractor(Protocol):
    name: str

    async def extract(self, ctx: MessageContext, box: Toolbox) -> Extraction: ...


_UNIT = r"(?:cases?|boxes|box|rolls?)"
_NUM = r"(?:\d+|[a-z]+)"
TABLE_ROW = re.compile(r"^\s*(\d+)\s*\|\s*([A-Za-z0-9-]+)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*([A-Za-z]+)\s*$")
BULLET = re.compile(r"^\s*[-*•]\s*(?P<ref>[^:\n]{2,80}):\s*(?P<qty>[^\n]{1,40}?)\s*$")
PHRASE = re.compile(
    rf"\b(?P<qty>{_NUM})\s+(?P<unit>{_UNIT})\s+of\s+(?P<item>[a-z0-9][a-z0-9 ,-]*?)(?=\s+and\s|\s+to\s|[.,;\n]|$)", re.I
)
AMEND_QTY = re.compile(
    rf"\bmake the (?P<item>[a-z0-9 ,-]+?) (?P<new>{_NUM} {_UNIT})\s*,?\s*(?:not|instead of) (?P<old>{_NUM})", re.I
)
AMEND_FROM_TO = re.compile(
    rf"\bchange the (?P<item>[a-z0-9 ,-]+?) from (?P<old>{_NUM} {_UNIT}) to (?P<new>{_NUM} {_UNIT})\b", re.I
)
AMEND_CHANGE = re.compile(
    rf"\bchange (?:line (?P<line>\d+)|the (?P<item>[a-z0-9 ,-]+?)) to (?P<new>{_NUM}(?: {_UNIT})?)\b", re.I
)
SHIP = re.compile(
    r"\b(?:ship|deliver)(?: it| this| them| everything| the order)? to (?P<dest>[^.\n]+?)(?:\s+instead\b[^.\n]*)?(?:[.\n]|$)",
    re.I,
)
TO_OUR = re.compile(r"\bto (?P<dest>our [a-z ]{2,40}?(?:dock|store|warehouse|office|site))\b", re.I)
CODE = re.compile(r"^[A-Z]{1,6}(?:-[A-Z0-9]+)+$")


class RuleBasedExtractor:
    name = "rules"

    async def extract(self, ctx: MessageContext, box: Toolbox) -> Extraction:  # noqa: ARG002
        msg = ctx.message
        body = msg.body_text

        def ev_body(excerpt: str) -> Evidence:
            return Evidence(message_id=msg.message_id, location="body", excerpt=excerpt.strip())

        po, po_ev = None, None
        for loc, text, att in [("subject", msg.subject, None), ("body", body, None)] + [
            ("attachment", a.full_text(), a) for a in ctx.attachments
        ]:
            hits = find_po_numbers(text)
            if hits:
                po = hits[0][0]
                po_ev = Evidence(
                    message_id=msg.message_id,
                    location=loc,
                    attachment_id=att.attachment_id if att else None,
                    excerpt=hits[0][1],
                )
                break

        amendments: list[ExtractedAmendment] = []
        taken: list[tuple[int, int]] = []
        for m in AMEND_FROM_TO.finditer(body):
            taken.append(m.span())
            amendments.append(
                ExtractedAmendment(
                    field="quantity",
                    description=m.group("item"),
                    new_value_text=m.group("new"),
                    previous_value_text=m.group("old"),
                    evidence=ev_body(m.group(0)),
                )
            )
        for m in AMEND_QTY.finditer(body):
            amendments.append(
                ExtractedAmendment(
                    field="quantity",
                    description=m.group("item"),
                    new_value_text=m.group("new"),
                    previous_value_text=m.group("old"),
                    evidence=ev_body(m.group(0)),
                )
            )
        for m in AMEND_CHANGE.finditer(body):
            if any(a <= m.start() < b for a, b in taken):
                continue  # already read as "change X from A to B"
            amendments.append(
                ExtractedAmendment(
                    field="quantity",
                    line_number=int(m.group("line")) if m.group("line") else None,
                    description=m.group("item"),
                    new_value_text=m.group("new"),
                    evidence=ev_body(m.group(0)),
                )
            )

        ship_text, ship_ev = None, None
        ship_m = SHIP.search(body) or TO_OUR.search(body)
        if ship_m:
            ship_text, ship_ev = ship_m.group("dest").strip(), ev_body(ship_m.group(0))

        if amendments:
            if ship_text:
                amendments.append(ExtractedAmendment(field="ship_to", new_value_text=ship_text, evidence=ship_ev))
            return Extraction(intent="amendment", po_number=po, po_evidence=po_ev, amendments=amendments)

        lines = self._table_lines(ctx) or self._bullet_lines(body, ev_body) or self._phrase_lines(body, ev_body)
        if not lines and po is None:
            return Extraction(intent="not_an_order")
        return Extraction(
            intent="new_order",
            po_number=po,
            po_evidence=po_ev,
            ship_to_text=ship_text,
            ship_to_evidence=ship_ev,
            lines=lines,
        )

    @staticmethod
    def _table_lines(ctx: MessageContext) -> list[ExtractedLine]:
        out = []
        for att in ctx.attachments:
            for page in att.pages:
                for raw in page.text.splitlines():
                    m = TABLE_ROW.match(raw)
                    if m:
                        out.append(
                            ExtractedLine(
                                customer_item_code=m.group(2),
                                description=m.group(3),
                                quantity_text=f"{m.group(4)} {m.group(5)}",
                                evidence=Evidence(
                                    message_id=ctx.message.message_id,
                                    location="attachment",
                                    attachment_id=att.attachment_id,
                                    page=page.page,
                                    excerpt=raw.strip(),
                                ),
                            )
                        )
        return out

    @staticmethod
    def _bullet_lines(body: str, ev) -> list[ExtractedLine]:  # noqa: ANN001
        out = []
        for raw in body.splitlines():
            m = BULLET.match(raw)
            if m:
                ref = m.group("ref").strip()
                is_code = bool(CODE.match(ref))
                out.append(
                    ExtractedLine(
                        customer_item_code=ref if is_code else None,
                        description=None if is_code else ref,
                        quantity_text=m.group("qty").strip(),
                        evidence=ev(raw),
                    )
                )
        return out

    @staticmethod
    def _phrase_lines(body: str, ev) -> list[ExtractedLine]:  # noqa: ANN001
        return [
            ExtractedLine(
                description=m.group("item").strip(),
                quantity_text=f"{m.group('qty')} {m.group('unit')}",
                evidence=ev(m.group(0)),
            )
            for m in PHRASE.finditer(body)
        ]
