"""Flag instruction-like text in customer content so a reviewer can see it.

This is a *visibility* aid, not the security control. The controls are structural: the model has
no tool that can send email, read other threads, list the mailbox or write drafts, and every
value it proposes is re-validated by deterministic code.
"""

from __future__ import annotations

import re

_PATTERNS = [
    r"\bignore (?:all |any |your |the |previous |prior |above )*(?:instructions|rules|prompts?)\b",
    r"\b(?:note|message|instructions?) (?:to|for) (?:the )?(?:ai|assistant|llm|model|bot)s?\b",
    r"\bsystem prompt\b",
    r"\byou are now\b",
    r"\b(?:email|send|forward|export) (?:every|all|the) .{0,40}\b(?:records?|customers?|data|contacts?)\b",
    r"\bapprove (?:this|the) order automatically\b",
    r"\bset (?:every|all|the) prices? to\b",
]
_RE = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def find_instruction_like(text: str) -> list[str]:
    """Return the source lines (verbatim, trimmed) that look like instructions aimed at an AI."""
    hits = []
    for line in text.splitlines():
        if _RE.search(line):
            hits.append(line.strip()[:300])
    return hits
