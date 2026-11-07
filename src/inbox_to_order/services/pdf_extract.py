"""Text extraction for text-based PDFs (pypdf). OCR is intentionally out of scope for this proof of concept."""

from __future__ import annotations

import io
import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ..config import MAX_ATTACHMENT_BYTES, MAX_PDF_PAGES, MAX_TEXT_CHARS
from ..models import AttachmentText, PageText

logging.getLogger("pypdf").setLevel(logging.ERROR)

NO_TEXT_DETAIL = (
    "This PDF has no extractable text layer (it looks like a scan or image). "
    "OCR is outside the scope of this proof of concept, so nothing was read from it. Ask the customer for a text PDF "
    "or enter the lines manually."
)


def extract_pdf_text(
    data: bytes, *, message_id: str, attachment_id: str, filename: str, mime_type: str
) -> AttachmentText:
    base = {"message_id": message_id, "attachment_id": attachment_id, "filename": filename}
    is_pdf = mime_type == "application/pdf" or filename.lower().endswith(".pdf")
    if not is_pdf or not data.startswith(b"%PDF"):
        return AttachmentText(
            **base, status="not_supported", detail=f"Only text-based PDF attachments are supported (got {mime_type})."
        )
    if len(data) > MAX_ATTACHMENT_BYTES:
        return AttachmentText(
            **base,
            status="too_large",
            detail=f"Attachment exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit.",
        )
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return AttachmentText(**base, status="not_supported", detail="Encrypted PDFs are not supported.")
        pages: list[PageText] = []
        total = 0
        truncated = len(reader.pages) > MAX_PDF_PAGES
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            text = (page.extract_text() or "").replace("\r\n", "\n").strip()
            if total + len(text) > MAX_TEXT_CHARS:
                text = text[: MAX_TEXT_CHARS - total]
                truncated = True
            total += len(text)
            pages.append(PageText(page=i, text=text))
            if total >= MAX_TEXT_CHARS:
                break
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        return AttachmentText(**base, status="error", detail=f"The PDF could not be parsed ({type(exc).__name__}).")
    if not any(p.text.strip() for p in pages):
        return AttachmentText(**base, status="no_text", detail=NO_TEXT_DETAIL)
    return AttachmentText(**base, status="ok", pages=pages, truncated=truncated)
