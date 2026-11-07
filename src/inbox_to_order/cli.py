"""Command line entry point:  uv run inbox-to-order <command>

Commands (all documented in README.md):
  init-db [--reset]        create the app database and the Demo ERP (seeded from fixtures/)
  demo [--extractor X]     process every fixture thread in order and print the outcome
  process THREAD_ID        process one thread (fixture or Gmail, per INBOX_MODE)
  threads                  list threads through the Mail MCP server
  drafts                   print the proposals waiting for (or past) operator review
  confirm DRAFT_ID         operator action: confirm a proposal and write it to the Demo ERP
  erp-orders               print the orders in the Demo ERP (via its MCP server)
  eval [--extractor X]     compare results on all fixtures with fixtures/expected_outcomes.json
  mcp-diagnose             prove the MCP boundary: discover tools on both servers and call some
  gmail-auth               run the local OAuth consent flow (gmail.readonly) for your test account
  web [--port N]           start the review UI and the Demo ERP screen on http://127.0.0.1:8700
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace

from . import db
from .config import Settings, load_settings
from .models import STATUS_LABELS
from .orchestrator import ThreadResult, process_thread
from .services import drafts

FIXTURE_ORDER = [
    "thread-A_initial_order",
    "thread-C_ambiguous_item",
    "thread-D_injection_attempt",
    "thread-E_duplicate_message",
    "thread-F_image_only_pdf",
    "thread-G_missing_customer_or_PO",
    "thread-H_erp_order_change",
]


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings()
    if getattr(args, "extractor", None):
        s = replace(s, extractor=args.extractor)
    return s


def _require_db(s: Settings) -> None:
    if not s.db_path.exists():
        sys.exit("Database not found. Run `uv run inbox-to-order init-db` first.")


def print_result(res: ThreadResult) -> None:
    print(f"\n=== {res.thread_id} ===")
    if res.error:
        print(f"  ERROR: {res.error}")
        return
    for o in res.outcomes:
        draft = (f" proposal #{o.draft_id}" + (f" v{o.version}" if o.version else "")) if o.draft_id else ""
        cost = ""
        if o.usage and o.usage.calls:
            cost = f", {o.usage.input_tokens}+{o.usage.output_tokens} tokens"
            cost += f", est. ${o.usage.cost_usd:.4f}" if o.usage.cost_usd is not None else ""
        print(f"  [{o.message_id}] {o.outcome.upper()}{draft}  (extractor: {o.extractor}, {o.seconds:.2f}s{cost})")
        for c in o.changes:
            where = f"line {c.line_no} " if c.line_no else ""
            print(f"      change {where}{c.field}: {c.before} -> {c.after}")
        for i in o.issues:
            print(f"      {'QUESTION' if i.severity == 'blocking' else 'note'} [{i.field}]: {i.message}")
        for n in o.notices:
            print(f"      note: {n}")
        for u in o.untrusted:
            print(f"      ignored untrusted text: {u!r}")
        if o.error:
            print(f"      error: {o.error}")


def cmd_init_db(args: argparse.Namespace) -> None:
    s = _settings(args)
    db.init_db(s, reset=args.reset)
    print(
        f"App database: {s.db_path}\nDemo ERP:     {s.erp_db_path}\n"
        f"({'reset to the fictional seed data' if args.reset else 'existing proposals and orders kept'})."
    )


def cmd_process(args: argparse.Namespace) -> None:
    s = _settings(args)
    _require_db(s)
    print(f"Inbox mode: {s.inbox_mode} | extractor: {s.resolved_extractor()}")
    print_result(asyncio.run(process_thread(s, args.thread_id)))


def cmd_demo(args: argparse.Namespace) -> None:
    s = _settings(args)
    if s.inbox_mode != "fixture":
        sys.exit("`demo` runs the local fixtures; set INBOX_MODE=fixture (the default).")
    db.init_db(s, reset=True)
    ex = s.resolved_extractor()
    print(
        f"Fresh demo database. Extractor: {ex}"
        + ("  (rule-based baseline, no AI)" if ex == "rules" else f"  (model {s.anthropic_model})")
    )
    from .mcp_client import open_toolbox

    async def run() -> None:
        # One MCP session for the whole demo: starting the two servers per conversation would take most of the time.
        async with open_toolbox() as box:
            for tid in FIXTURE_ORDER:
                print_result(await process_thread(s, tid, toolbox=box))
            print("\nReplaying thread A (idempotency check):")
            print_result(await process_thread(s, FIXTURE_ORDER[0], toolbox=box))

    asyncio.run(run())
    cmd_drafts(args)


def cmd_drafts(args: argparse.Namespace) -> None:
    s = _settings(args)
    _require_db(s)
    conn = db.connect(s.db_path)
    try:
        print("\nProposals (in this app; only 'Written to Demo ERP' ones reached the ERP):")
        for row in db.list_drafts(conn):
            d = drafts.get_draft_by_id(conn, row["draft_id"])
            assert d is not None
            erp_ref = f" -> {d.erp_order_id} v{d.erp_version}" if d.erp_order_id else ""
            print(
                f"  #{d.draft_id} {d.po_number} {d.customer_name} v{d.version} [{STATUS_LABELS[d.status]}]{erp_ref} "
                f"ship-to: {d.ship_to_one_line or '-'}"
            )
            for ln in d.lines:
                qty = f"{ln.quantity} {ln.unit or '?'}" if ln.quantity else (ln.raw_quantity_text or "?")
                print(
                    f"     {ln.line_no}. {ln.customer_item_code or ln.description or '?':<14} -> {ln.sku or '?':<8} {qty:<10} [{STATUS_LABELS[ln.status]}]"
                )
    finally:
        conn.close()


def cmd_confirm(args: argparse.Namespace) -> None:
    from .mcp_client import open_toolbox
    from .review import ReviewError, confirm_and_write

    s = _settings(args)
    _require_db(s)
    conn = db.connect(s.db_path)
    try:
        d = drafts.get_draft_by_id(conn, args.draft_id)
        if d is None:
            sys.exit(f"No proposal #{args.draft_id}.")
        version = args.version or d.version

        async def run() -> None:
            async with open_toolbox() as box:
                try:
                    res = await confirm_and_write(conn, box, d.draft_id, version)
                except ReviewError as exc:
                    print(f"Not written: {exc.code}: {exc.message}")
                    return
                print(f"{res.outcome.upper()}: {res.order.order_id} v{res.order.version} for {res.order.po_number}")
                for c in res.changes:
                    print(f"  {c}")

        asyncio.run(run())
    finally:
        conn.close()


def cmd_erp_orders(args: argparse.Namespace) -> None:
    from .mcp_client import open_toolbox

    s = _settings(args)
    _require_db(s)

    async def run() -> None:
        async with open_toolbox() as box:
            res = await box.call("list_orders", {"limit": 50})
            print("Demo ERP orders (fictional):")
            for o in res["orders"]:
                print(
                    f"  {o['order_id']} {o['po_number']} {o['customer_name']} v{o['version']} total ${o['total_usd']}  "
                    f"ship-to: {o['ship_to']['one_line']}"
                )
                for ln in o["lines"]:
                    print(f"     {ln['line_no']}. {ln['sku']:<8} {ln['quantity']} {ln['unit']:<5} {ln['name']}")

    asyncio.run(run())


def cmd_threads(args: argparse.Namespace) -> None:
    from .mcp_client import open_toolbox

    async def run() -> None:
        async with open_toolbox() as box:
            res = await box.call("list_order_threads", {"limit": 25})
            print(f"Provider: {res['provider']}")
            for t in res["threads"]:
                print(f"  {t['thread_id']:<36} {t['message_count']} msg, {t['attachment_count']} att  {t['subject']}")

    asyncio.run(run())


def cmd_mcp_diagnose(args: argparse.Namespace) -> None:
    from .mcp_client import MODEL_ALLOWED_TOOLS, SERVER_MODULES, open_toolbox

    s = _settings(args)
    _require_db(s)

    async def run() -> None:
        calls: list[str] = []
        async with open_toolbox(
            lambda actor, server, tool, a, status: calls.append(f"{actor} -> {server}.{tool}: {status}")
        ) as box:
            for name, client in box.clients.items():
                info = client.server_info
                print(
                    f"Server '{name}' ({SERVER_MODULES[name]}) over stdio, protocol {client.protocol_version}, "
                    f"reports itself as {info.name if info else '?'}"
                )
            print("\nDiscovered tools (tools/list):")
            for tool_name in box.tool_names():
                server, tool = box.tools[tool_name]
                ro = tool.annotations.read_only_hint if tool.annotations else None
                scope = "model+app" if tool_name in MODEL_ALLOWED_TOOLS else "app only"
                print(f"  {server:<5} {tool_name:<20} read_only={ro!s:<5} available to: {scope}")
            print("\nInvoking tools across the protocol boundary (tools/call):")
            threads = await box.call("list_order_threads", {"limit": 3})
            print(
                f"  mail.list_order_threads -> {len(threads['threads'])} thread(s) from provider '{threads['provider']}'"
            )
            prod = await box.call(
                "find_product", {"customer_id": "PINE-RIDGE", "customer_code_or_description": "PR-TAPE-2"}
            )
            print(
                f"  erp.find_product('PR-TAPE-2') -> {prod['status']} {[c['sku'] for c in prod['candidates']]} via {prod['match_type']}"
            )
            orders = await box.call("list_orders", {"limit": 5})
            print(f"  erp.list_orders -> {len(orders['orders'])} order(s)")
            print("\nThe mail server has no tool that sends, modifies, labels or deletes email.")
            print("Demo ERP writes (create_order, update_order) are called only by the app after an operator confirms.")
        print("\nAudit hook saw:", *calls, sep="\n  ")

    asyncio.run(run())


def cmd_eval(args: argparse.Namespace) -> None:
    from .evaluation import evaluate_fixtures

    s = _settings(args)
    if s.inbox_mode != "fixture":
        sys.exit("`eval` runs the local fixtures; set INBOX_MODE=fixture (the default).")
    ex = s.resolved_extractor()
    if ex == "claude":
        print(f"Live evaluation with {s.anthropic_model}: this makes paid API calls and resets the local database.")
    report = asyncio.run(evaluate_fixtures(s))
    calls = inp = outp = 0
    cost = 0.0
    for o in report.outcomes:
        u = o.usage
        extra = ""
        if u and u.calls:
            calls, inp, outp = calls + u.calls, inp + u.input_tokens, outp + u.output_tokens
            cost += u.cost_usd or 0.0
            extra = f"  {u.calls} call(s), {u.input_tokens}+{u.output_tokens} tokens" + (
                f", est. ${u.cost_usd:.4f}" if u.cost_usd is not None else ""
            )
        print(f"  {o.message_id:<26} {o.outcome:<11} {o.seconds:6.2f}s{extra}")
    print(
        f"Extractor: {report.extractor} | checks: {report.checks} | failed: {len(report.failures)} | wall time: {report.seconds:.1f}s"
    )
    if calls:
        print(
            f"Model: {s.anthropic_model} | {calls} API calls | {inp} input + {outp} output tokens | "
            f"estimated ${cost:.4f} at list price"
        )
    for f in report.failures:
        print(f"  FAIL {f}")
    raise SystemExit(0 if report.passed else 1)


def cmd_gmail_auth(args: argparse.Namespace) -> None:
    from .services.gmail_auth import GmailNotConfigured, run_oauth_flow

    s = _settings(args)
    print("Requesting scope https://www.googleapis.com/auth/gmail.readonly (Google classifies it as restricted).")
    print("Use a dedicated test mailbox containing only synthetic order emails.")
    try:
        path = run_oauth_flow(s)
    except GmailNotConfigured as exc:
        sys.exit(str(exc))
    print(f"Authorized. Token stored at {path} (git-ignored). Set INBOX_MODE=gmail to use it.")


def cmd_web(args: argparse.Namespace) -> None:
    from .web.server import serve

    print(
        f"Inbox to Order: http://127.0.0.1:{args.port}   Demo ERP: http://127.0.0.1:{args.port}/erp   (Ctrl+C to stop)"
    )
    serve(args.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="inbox-to-order", description="Inbox to Order - a proof of concept")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("init-db", help="create and seed the SQLite database")
    sp.add_argument("--reset", action="store_true", help="delete existing drafts and history")
    sp.set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("demo", help="process all fixture threads on a fresh database")
    sp.add_argument("--extractor", choices=["auto", "claude", "rules"])
    sp.set_defaults(fn=cmd_demo)
    sp = sub.add_parser("process", help="process one thread")
    sp.add_argument("thread_id")
    sp.add_argument("--extractor", choices=["auto", "claude", "rules"])
    sp.set_defaults(fn=cmd_process)
    sub.add_parser("threads", help="list threads via MCP").set_defaults(fn=cmd_threads)
    sub.add_parser("drafts", help="print proposals").set_defaults(fn=cmd_drafts)
    sp = sub.add_parser("confirm", help="operator: confirm a proposal and write it to the Demo ERP")
    sp.add_argument("draft_id", type=int)
    sp.add_argument("--version", type=int, help="the proposal version you reviewed (default: current)")
    sp.set_defaults(fn=cmd_confirm)
    sub.add_parser("erp-orders", help="print Demo ERP orders").set_defaults(fn=cmd_erp_orders)
    sp = sub.add_parser("eval", help="check all fixtures against expected outcomes (resets the database)")
    sp.add_argument("--extractor", choices=["auto", "claude", "rules"])
    sp.set_defaults(fn=cmd_eval)
    sub.add_parser("mcp-diagnose", help="show MCP discovery and a real tool call").set_defaults(fn=cmd_mcp_diagnose)
    sub.add_parser("gmail-auth", help="authorize a Gmail test account (read-only)").set_defaults(fn=cmd_gmail_auth)
    sp = sub.add_parser("web", help="start the local review UI (React) and the Demo ERP screen")
    sp.add_argument("--port", type=int, default=8700)
    sp.set_defaults(fn=cmd_web)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
