"""The React UI in a real (headless) browser against the real server and MCP servers, rule-based reading.

Skipped when Playwright's Chromium is not installed (python -m playwright install chromium) or the UI is
not built (scripts/build-ui.sh).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from inbox_to_order.web.server import DIST, create_app

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not (DIST / "index.html").exists(), reason="web UI not built")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(settings):
    import uvicorn

    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(settings, port=port), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(10)


def test_queue_review_confirm_and_the_erp_window(server, erp_conn):
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not installed: {exc}")
        ctx = browser.new_context(locale="en-US")
        page = ctx.new_page()
        page.goto(server + "/")
        page.wait_for_selector(".lane-new .card", timeout=60000)
        assert page.locator(".lane-new .card").count() >= 6

        erp = ctx.new_page()
        erp.goto(server + "/erp")
        erp.wait_for_selector(".erp-list tbody tr")
        assert erp.locator(".erp-list tbody tr").count() == 1  # the seed order only

        page.goto(server + "/#/t/thread-A_initial_order")
        page.get_by_role("button", name="Prepare proposal").click()
        page.wait_for_selector("button:has-text('Analyze update'):not([disabled])", timeout=120000)
        assert page.locator(".draft-needs_review, .draft-ready").count() == 1
        page.get_by_role("button", name="Analyze update").click()
        page.wait_for_selector(".change", timeout=120000)
        assert "8 case" in page.locator(".change").inner_text()

        page.hover(".lrow:not(.lhead) >> nth=0")
        assert page.locator("mark.on").count() >= 1  # the line's evidence lights up in the source

        page.get_by_role("button", name="Confirm & write to Demo ERP").click()
        page.wait_for_selector(".stamp", timeout=60000)
        assert "SO-1001" in page.locator(".stamp").inner_text()
        assert (
            erp_conn.execute("SELECT quantity FROM order_lines WHERE order_id='SO-1001' AND line_no=1").fetchone()[0]
            == 8
        )

        # the ERP window updates without a reload, at once (announced by the app, not left to its 2 s timer)
        erp.wait_for_selector("tr.fresh >> text=SO-1001", timeout=1500)
        page.goto(server + "/#/")
        page.wait_for_selector(".lane-erp .card")
        assert "SO-1001" in page.locator(".lane-erp .card").first.inner_text()
        browser.close()
