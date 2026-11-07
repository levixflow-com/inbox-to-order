"""Optional live checks. Excluded by default; run explicitly with:  uv run pytest -m live

- test_live_claude_fixture_eval: real, paid Claude API calls on all fixtures (needs ANTHROPIC_API_KEY).
- test_live_gmail_listing: lists threads from YOUR Gmail test account (needs secrets/token.json).

Results of live runs are observations about one model/mailbox at one point in time, not guarantees.
"""

from __future__ import annotations

import os

import pytest

from inbox_to_order.agent import ClaudeExtractor
from inbox_to_order.config import load_settings
from inbox_to_order.evaluation import evaluate_fixtures

load_settings()  # loads .env into the environment, if present
_KEY = os.environ.get("ANTHROPIC_API_KEY")  # captured before the `settings` fixture clears it

pytestmark = pytest.mark.live


@pytest.mark.anyio
@pytest.mark.skipif(not _KEY, reason="ANTHROPIC_API_KEY not set")
async def test_live_claude_fixture_eval(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY or "")
    report = await evaluate_fixtures(settings, ClaudeExtractor(settings))
    print(f"\nlive eval: {report.checks} checks, {len(report.failures)} failed, {report.seconds:.1f}s")
    for f in report.failures:
        print("  FAIL", f)
    assert report.passed, report.failures


@pytest.mark.skipif(
    not load_settings().gmail_token_file.exists(), reason="no Gmail token; run `inbox-to-order gmail-auth`"
)
def test_live_gmail_listing():
    from dataclasses import replace

    from inbox_to_order.services.inbox_sources import GmailInbox

    inbox = GmailInbox(replace(load_settings(), inbox_mode="gmail"))
    listing = inbox.list_threads(None, 5)
    assert listing.provider == "gmail"
    if listing.threads:
        thread = inbox.get_thread(listing.threads[0].thread_id)
        assert thread.messages and all(m.provider == "gmail" for m in thread.messages)
