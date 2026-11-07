"""Quantity and unit parsing. Plain code, not the model: "eight cases" -> (8, "case").

No unit conversion is ever performed. If the stated unit differs from the product's selling unit,
or no unit is stated, the line goes to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import MAX_LINE_QUANTITY

_ONES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

UNIT_ALIASES = {
    "case": "case",
    "cases": "case",
    "cs": "case",
    "box": "box",
    "boxes": "box",
    "bx": "box",
    "roll": "roll",
    "rolls": "roll",
    "rl": "roll",
    # Recognized but never converted: they will not equal a selling unit of case/box/roll.
    "each": "each",
    "ea": "each",
    "unit": "each",
    "units": "each",
    "piece": "each",
    "pieces": "each",
    "pallet": "pallet",
    "pallets": "pallet",
    "carton": "carton",
    "cartons": "carton",
}


@dataclass(frozen=True)
class ParsedQuantity:
    quantity: int | None
    unit: str | None
    problem: str | None = None


def _words_to_int(words: list[str]) -> int | None:
    total, current = 0, 0
    if not words:
        return None
    for w in words:
        if w in _ONES:
            current += _ONES[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred" and current:
            current *= 100
        elif w == "thousand" and current:
            total += current * 1000
            current = 0
        elif w == "and":
            continue
        else:
            return None
    return total + current


def parse_quantity(text: str | None) -> ParsedQuantity:
    if not text or not text.strip():
        return ParsedQuantity(None, None, "No quantity was stated.")
    cleaned = re.sub(r"[()\[\],.;:]", " ", text.lower().replace("-", " "))
    tokens = [t for t in cleaned.split() if t not in {"of", "x", "qty", "quantity", "the", "to", "a"}]

    unit = None
    unit_tokens = [t for t in tokens if t in UNIT_ALIASES]
    if len({UNIT_ALIASES[t] for t in unit_tokens}) > 1:
        return ParsedQuantity(None, None, f"More than one unit mentioned in {text!r}.")
    if unit_tokens:
        unit = UNIT_ALIASES[unit_tokens[0]]
    rest = [t for t in tokens if t not in UNIT_ALIASES]

    qty: int | None = None
    digits = [t for t in rest if re.fullmatch(r"\d+", t)]
    if len(digits) == 1 and len(rest) == 1:
        qty = int(digits[0])
    elif not digits:
        qty = _words_to_int(rest)
    if qty is None:
        return ParsedQuantity(None, unit, f"Could not read a single whole-number quantity from {text!r}.")
    if qty <= 0 or qty > MAX_LINE_QUANTITY:
        return ParsedQuantity(None, unit, f"Quantity {qty} is outside the allowed range 1-{MAX_LINE_QUANTITY}.")
    if unit is None:
        return ParsedQuantity(qty, None, "No selling unit was stated.")
    return ParsedQuantity(qty, unit, None)
