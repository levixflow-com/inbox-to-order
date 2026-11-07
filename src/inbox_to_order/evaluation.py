"""Compare an extractor's end-to-end results on the fixtures with fixtures/expected_outcomes.json.

Used by the offline tests (rule-based extractor) and by `uv run inbox-to-order eval`, which runs
whichever extractor is configured - with an API key, that is a live, paid model evaluation.
For the C fixture the first line must stay unresolved: no SKU and no unit may be filled in.
Processing alone must never write to the Demo ERP; that is checked too.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import db
from .config import Settings
from .extractors import Extractor
from .mcp_client import Toolbox, open_toolbox
from .orchestrator import MessageOutcome, process_thread
from .services import drafts, erp


@dataclass
class EvalReport:
    extractor: str
    checks: int = 0
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    outcomes: list[MessageOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _check(report: EvalReport, ok: bool, msg: str) -> None:
    report.checks += 1
    if not ok:
        report.failures.append(msg)


async def evaluate_fixtures(settings: Settings, extractor: Extractor | None = None) -> EvalReport:
    """Process each fixture message one at a time (like the UI) and check the state after each step."""
    db.init_db(settings, reset=True)
    async with open_toolbox() as box:
        return await _evaluate(settings, extractor, box)


async def _evaluate(settings: Settings, extractor: Extractor | None, box: Toolbox) -> EvalReport:
    spec = json.loads((settings.fixtures_dir / "expected_outcomes.json").read_text(encoding="utf-8"))
    started = time.perf_counter()
    erp_before = _erp_snapshot(settings)
    report = EvalReport(extractor=extractor.name if extractor else settings.resolved_extractor())
    for thread_id in spec["order"]:
        for exp in spec["threads"][thread_id]:
            mid = exp["message_id"]
            result = await process_thread(settings, thread_id, extractor=extractor, toolbox=box, max_messages=1)
            _check(report, result.error is None, f"{thread_id}: processing error {result.error}")
            report.outcomes.extend(result.outcomes)
            by_id: dict[str, MessageOutcome] = {o.message_id: o for o in result.outcomes}
            out = by_id.get(mid)
            if out is None:
                _check(report, False, f"{mid}: not processed")
                continue
            _check(
                report,
                out.outcome == exp["outcome"],
                f"{mid}: outcome {out.outcome!r}, expected {exp['outcome']!r} ({out.error or ''})",
            )
            if exp.get("min_untrusted"):
                _check(
                    report,
                    len(out.untrusted) >= exp["min_untrusted"],
                    f"{mid}: only {len(out.untrusted)} untrusted passage(s) flagged",
                )
            if exp.get("no_draft"):
                _check(report, out.draft_id is None, f"{mid}: a draft was created but none was expected")
            if "changes" in exp:
                got = [
                    {"field": c.field, "line_no": c.line_no, "before": c.before, "after": c.after} for c in out.changes
                ]
                _check(report, got == exp["changes"], f"{mid}: changes {got}, expected {exp['changes']}")
            if out.draft_id is None or "lines" not in exp:
                continue
            conn = db.connect(settings.db_path)
            try:
                draft = drafts.get_draft_by_id(conn, out.draft_id)
            finally:
                conn.close()
            assert draft is not None
            if "po_number" in exp:
                _check(report, draft.po_number == exp["po_number"], f"{mid}: PO {draft.po_number}")
            if "version" in exp:
                _check(report, draft.version == exp["version"], f"{mid}: version {draft.version}")
            if "ship_to" in exp:
                _check(
                    report,
                    draft.ship_to.address_id == exp["ship_to"] and draft.ship_to.status == "matched",
                    f"{mid}: ship-to {draft.ship_to.address_id} ({draft.ship_to.status})",
                )
            got_lines = [[ln.sku, ln.quantity if ln.unit else None, ln.unit] for ln in draft.lines]
            want = exp["lines"]
            _check(report, len(got_lines) == len(want), f"{mid}: {len(got_lines)} lines, expected {len(want)}")
            for i, (g, w) in enumerate(zip(got_lines, want, strict=False), start=1):
                _check(report, g == w, f"{mid}: line {i} is {g}, expected {w}")
            _check(
                report,
                draft.status == exp["draft_status"],
                f"{mid}: draft status {draft.status}, expected {exp['draft_status']}",
            )
            if "question_contains" in exp:
                text = " ".join(i.message for i in draft.issues).lower()
                _check(
                    report,
                    exp["question_contains"].lower() in text,
                    f"{mid}: no question mentioning {exp['question_contains']!r}",
                )
    # Replaying everything must change nothing.
    conn = db.connect(settings.db_path)
    try:
        before = conn.execute(
            "SELECT (SELECT COUNT(*) FROM order_drafts), (SELECT COUNT(*) FROM draft_revisions)"
        ).fetchone()
    finally:
        conn.close()
    for thread_id in spec["order"]:
        await process_thread(settings, thread_id, extractor=extractor, toolbox=box)
    conn = db.connect(settings.db_path)
    try:
        after = conn.execute(
            "SELECT (SELECT COUNT(*) FROM order_drafts), (SELECT COUNT(*) FROM draft_revisions)"
        ).fetchone()
    finally:
        conn.close()
    _check(report, tuple(before) == tuple(after), f"replay changed drafts/revisions {tuple(before)} -> {tuple(after)}")
    _check(
        report, _erp_snapshot(settings) == erp_before, "the Demo ERP changed although no operator confirmed anything"
    )
    report.seconds = time.perf_counter() - started
    return report


def _erp_snapshot(settings: Settings) -> tuple:
    conn = erp.open_erp(settings.erp_db_path)
    try:
        return tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM orders), (SELECT COALESCE(SUM(version),0) FROM orders), (SELECT COUNT(*) FROM order_history)"
            ).fetchone()
        )
    finally:
        conn.close()
