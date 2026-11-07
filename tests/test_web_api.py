"""The local web API, with the real MCP server subprocesses: read, review, edit, confirm, plus the
checks a browser-facing local API needs (token, Host, Origin)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from inbox_to_order.web.server import create_app

TOKEN = "test-token"
THREAD = "thread-A_initial_order"


@pytest.fixture
def client(settings):
    app = create_app(settings, token=TOKEN, extra_hosts=("testserver",))
    with TestClient(app, headers={"X-Api-Token": TOKEN}) as c:
        yield c


def _process(client: TestClient) -> list[dict]:
    r = client.post(f"/api/threads/{THREAD}/process", json={"ack": False})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line]


def test_prepare_amend_confirm_through_the_api(client, conn, erp_conn):
    q = client.get("/api/queue").json()
    a = next(t for t in q["threads"] if t["thread_id"] == THREAD)
    assert a["lane"] == "new" and q["lanes"][0] == "new"
    assert client.get(f"/api/threads/{THREAD}").json()["action"] == "Prepare proposal"

    events = _process(client)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "done" and "step" in kinds  # steps arrive while the run is going
    run = events[-1]["run"]
    assert run["outcomes"][0]["outcome"] == "created"
    assert {c["tool"] for c in run["calls"]} >= {"get_order_thread", "find_customer"}
    assert erp_conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1  # only the seed order

    assert _process(client)[-1]["run"]["outcomes"][0]["outcome"] == "amended"
    detail = client.get(f"/api/threads/{THREAD}").json()
    d = detail["drafts"][0]
    assert (d["version"], d["status"], detail["lane"]) == (2, "ready", "ready")
    assert d["latest_change"]["changes"][0]["after"].startswith("8")
    assert detail["last_run"]["outcomes"][0]["outcome"] == "amended" and not detail["unprocessed"]

    stale = client.post(f"/api/drafts/{d['draft_id']}/confirm", json={"version": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    ok = client.post(f"/api/drafts/{d['draft_id']}/confirm", json={"version": 2}).json()
    assert (ok["outcome"], ok["order_id"], ok["version"]) == ("created", "SO-1001", 1)
    assert (
        erp_conn.execute("SELECT quantity FROM order_lines WHERE order_id='SO-1001' AND line_no=1").fetchone()[0] == 8
    )
    assert [r[0] for r in conn.execute("SELECT actor FROM audit_events WHERE event='confirmed_erp_created'")] == [
        "operator"
    ]

    erp = client.get("/erp/api/orders").json()
    so = next(o for o in erp["orders"] if o["order_id"] == "SO-1001")
    assert so["history"][0]["action"] == "created" and erp["last_change"]
    assert client.get("/api/queue").json()["threads"][0]["lane"] in {"erp", "new", "review", "ready", "not_order"}


def test_edit_and_reject_need_the_current_version(client):
    _process(client)
    d = client.get(f"/api/threads/{THREAD}").json()["drafts"][0]
    ch = client.get(f"/api/drafts/{d['draft_id']}/choices").json()
    assert ch["products"] and ch["addresses"]
    body = {
        "version": d["version"],
        "address_id": ch["addresses"][0]["address_id"],
        "lines": [{"sku": ln["sku"], "quantity": ln["quantity"] + 1} for ln in d["lines"]],
        "note": "test",
    }
    edited = client.post(f"/api/drafts/{d['draft_id']}/edit", json=body)
    assert edited.status_code == 200 and edited.json()["version"] == d["version"] + 1
    assert client.post(f"/api/drafts/{d['draft_id']}/reject", json={"version": d["version"]}).status_code == 409
    assert (
        client.post(f"/api/drafts/{d['draft_id']}/reject", json={"version": d["version"] + 1}).json()["status"]
        == "rejected"
    )


def test_a_save_without_changes_keeps_showing_the_last_real_change(client):
    _process(client)
    _process(client)  # v2: tape 6 -> 8 cases, from the customer's reply
    d = client.get(f"/api/threads/{THREAD}").json()["drafts"][0]
    ch = client.get(f"/api/drafts/{d['draft_id']}/choices").json()
    same = {
        "version": d["version"],
        "address_id": d["ship_to"]["address_id"],
        "lines": [{"sku": ln["sku"], "quantity": ln["quantity"]} for ln in d["lines"]],
        "note": "",
    }
    assert ch["addresses"] and client.post(f"/api/drafts/{d['draft_id']}/edit", json=same).json()["version"] == 3
    after = client.get(f"/api/threads/{THREAD}").json()
    lc = after["drafts"][0]["latest_change"]
    assert (lc["version"], lc["checked_in"]) == (2, 3) and lc["changes"][0]["after"].startswith("8")
    assert "quantity" in after["reason"] and after["drafts"][0]["revisions"][-1]["changes"] == [
        "checked, no value changed"
    ]


def test_a_conversation_outside_the_current_inbox_is_refused_before_any_mail_call(client):
    # e.g. a demo-email link opened while Gmail is selected: never passed on to the mailbox
    for r in (
        client.get("/api/threads/1a1b1c7cde7877a6"),
        client.post("/api/threads/1a1b1c7cde7877a6/process", json={"ack": True}),
    ):
        assert r.status_code == 404 and r.json()["code"] == "NOT_IN_INBOX", r.text


def test_switching_inbox_while_servers_start_keeps_each_session_on_its_inbox(settings, monkeypatch):
    import os

    from inbox_to_order.web import server

    rt = server.Runtime(settings)

    class SlowSession:  # the operator switches to Gmail while the demo inbox's servers are starting
        def __init__(self) -> None:
            self.inbox = os.environ["INBOX_MODE"]
            rt.mode = "gmail"

    monkeypatch.setattr(server, "McpSession", SlowSession)
    rt.mode = "fixture"
    assert rt.session("fixture").inbox == "fixture"
    assert rt.session("gmail").inbox == "gmail"  # a new session, not the demo one filed under "gmail"


def test_requests_without_token_or_from_other_sites_are_refused(settings):
    app = create_app(settings, token=TOKEN, extra_hosts=("testserver",))
    with TestClient(app) as c:
        assert c.get("/api/status").json()["code"] == "BAD_TOKEN"
        assert c.get("/erp/api/orders", headers={"X-Api-Token": "wrong"}).status_code == 403
        evil = c.post(
            f"/api/threads/{THREAD}/process", json={}, headers={"X-Api-Token": TOKEN, "Origin": "https://evil.example"}
        )
        assert evil.status_code == 403 and evil.json()["code"] == "BAD_ORIGIN"
        assert c.get("/api/status", headers={"X-Api-Token": TOKEN, "Host": "evil.example"}).json()["code"] == "BAD_HOST"
        assert c.get("/api/status", headers={"X-Api-Token": TOKEN}).json()["extractor"] == "rules"


def test_claude_runs_need_the_data_sharing_ack(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    from dataclasses import replace

    from inbox_to_order.config import load_settings

    s = replace(load_settings(), db_path=settings.db_path, erp_db_path=settings.erp_db_path)
    app = create_app(s, token=TOKEN, extra_hosts=("testserver",))
    with TestClient(app, headers={"X-Api-Token": TOKEN}) as c:
        c.post("/api/settings", json={"extractor": "claude"})
        r = c.post(f"/api/threads/{THREAD}/process", json={"ack": False})
        assert r.status_code == 400 and r.json()["code"] == "ACK_REQUIRED"


def test_two_conversations_are_read_at_the_same_time_each_with_its_own_log(client, settings):
    # "Read all new" reads several conversations at once over one MCP session; each run logs its own tool calls.
    from concurrent.futures import ThreadPoolExecutor

    ids = [t["thread_id"] for t in client.get("/api/queue").json()["threads"] if t["lane"] == "new"][:2]
    assert len(ids) == 2

    def process(thread_id: str) -> dict:
        r = client.post(f"/api/threads/{thread_id}/process", json={"ack": False})
        return [json.loads(line) for line in r.text.splitlines() if line][-1]

    with ThreadPoolExecutor(2) as pool:
        last = list(pool.map(process, ids))
    assert [e["type"] for e in last] == ["done", "done"], last
    for e in last:
        tools = [c["tool"] for c in e["run"]["calls"]]
        assert tools.count("get_order_thread") == 1 and tools.count("find_customer") == 1, tools

    from inbox_to_order.web import server

    rt = server.Runtime(settings)  # one conversation is still read one run at a time
    assert rt.start_run(("fixture", ids[0])) and not rt.start_run(("fixture", ids[0]))
    assert rt.start_run(("fixture", ids[1]))
    rt.end_run(("fixture", ids[0]))
    assert rt.start_run(("fixture", ids[0]))
