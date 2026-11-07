"""Capture the README screenshots by driving the real app (React UI) in a browser, on the demo emails.

Run:  uv run python scripts/capture_screenshots.py [--channel msedge|chrome|chromium]

It starts its own server on a free 127.0.0.1 port with throw-away databases (data/screenshots*.db), clicks
through the demo like a user would, and writes PNGs to docs/screenshots/. Reading is forced to the rule-based
baseline and the AI key is blanked, so nothing is sent anywhere and the UI says "Read by rules, no AI".
Nothing here fabricates UI state: every picture is the app as it is after the clicks.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="msedge", help="Playwright browser channel (msedge, chrome, or chromium)")
    args = ap.parse_args()

    db_path, erp_path = ROOT / "data" / "screenshots.db", ROOT / "data" / "screenshots_erp.db"
    # Set before the settings are loaded: .env never overrides a variable that is already set, and the MCP server
    # processes inherit these. An empty key means "no AI".
    os.environ.update(
        INBOX_MODE="fixture",
        EXTRACTOR="rules",
        ANTHROPIC_API_KEY="",
        ANTHROPIC_AUTH_TOKEN="",
        INBOX_TO_ORDER_DB=str(db_path),
        DEMO_ERP_DB=str(erp_path),
        EXTRA_CUSTOMER_SENDERS="",
    )
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn
    from playwright.sync_api import sync_playwright

    from inbox_to_order.config import load_settings
    from inbox_to_order.db import init_db
    from inbox_to_order.web.server import create_app

    settings = replace(load_settings(), db_path=db_path, erp_db_path=erp_path, fixtures_dir=ROOT / "fixtures")
    init_db(settings, reset=True)
    port = free_port()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(settings, port=port), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    OUT.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"channel": args.channel} if args.channel != "chromium" else {}))
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US", reduced_motion="reduce")
            page = ctx.new_page()

            def shot(target, name: str) -> None:
                target.wait_for_timeout(600)
                target.screenshot(path=str(OUT / name))
                print("saved", name)

            # 1. The inbox as it arrives, then after "Read all new"
            page.goto(base + "/")
            page.wait_for_selector(".lane-new .card", timeout=60_000)
            shot(page, "01_inbox_new.png")
            page.get_by_role("button", name="Read all new").click()
            page.wait_for_selector(".lane-new .card", state="detached", timeout=120_000)
            page.wait_for_selector(".card.running", state="detached", timeout=120_000)
            shot(page, "02_inbox_sorted.png")

            # 2. One proposal: the customer's change, with the evidence of the first line lit up in the email
            page.goto(base + "/#/t/thread-A_initial_order")
            page.wait_for_selector(".change", timeout=60_000)
            page.hover(".lrow:not(.lhead) >> nth=0")
            page.wait_for_selector("mark.on")
            shot(page, "03_proposal_with_evidence.png")

            # 3. A question for a person: an item that matches two catalog entries
            page.goto(base + "/#/t/thread-C_ambiguous_item")
            page.wait_for_selector(".questions", timeout=60_000)
            shot(page, "04_question_for_a_person.png")

            # 4. Hidden instructions in the email and the PDF: shown to the operator, ignored
            page.goto(base + "/#/t/thread-D_injection_attempt")
            page.wait_for_selector(".lrow", timeout=60_000)
            page.locator(".warn").first.evaluate("el => el.scrollIntoView({block: 'end'})")
            shot(page, "05_hidden_instructions_ignored.png")

            # 5. Confirm, and the Demo ERP window updates by itself
            erp = ctx.new_page()
            erp.goto(base + "/erp")
            erp.wait_for_selector(".erp-list tbody tr")
            page.goto(base + "/#/t/thread-A_initial_order")
            page.get_by_role("button", name="Confirm & write to Demo ERP").click()
            page.wait_for_selector(".stamp", timeout=60_000)
            shot(page, "06_confirmed.png")
            erp.wait_for_selector("tr.fresh >> text=SO-1001", timeout=10_000)
            erp.locator("tr.fresh >> text=SO-1001").click()
            shot(erp, "07_demo_erp.png")
            browser.close()
    finally:
        srv.should_exit = True


if __name__ == "__main__":
    main()
