"""Small text helpers shared by matching, evidence verification and the rule-based extractor."""

from __future__ import annotations

import re

_STOP = {"the", "of", "a", "an", "our", "in", "x", "and", "for", "size", "with", "to", "please", "pcs", "make"}


def tokens(text: str) -> set[str]:
    """Lower-case word tokens, stop words removed, naive plural folding ('labels' -> 'label')."""
    out = set()
    for tok in re.findall(r"[a-z0-9.]+", text.lower()):
        tok = tok.strip(".")
        if not tok or tok in _STOP:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return out


def norm(text: str) -> str:
    """Case- and whitespace-insensitive form used to verify that an excerpt really is in the source."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


PO_REGEX = re.compile(r"\bP\.?O\.?\s*(?:#|No\.?|Number)?\s*[-:#]?\s*(\d{4,10})\b", re.IGNORECASE)


def find_po_numbers(text: str) -> list[tuple[str, str]]:
    """Return (normalized 'PO-12345', matched excerpt) pairs in order of appearance."""
    return [(f"PO-{m.group(1)}", m.group(0)) for m in PO_REGEX.finditer(text)]


def normalize_po(value: str) -> str:
    m = PO_REGEX.search(value)
    if m:
        return f"PO-{m.group(1)}"
    return re.sub(r"\s+", "-", value.strip().upper())
