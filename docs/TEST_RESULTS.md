# Test results

What has been run, how, and what it shows. All numbers below were measured on the author's Windows 11 machine;
costs are estimates from the token counts the API returned, at list price. They are one person's measurements on
test data, not a benchmark.

## Offline tests

```bash
uv run pytest                 # no key, no Google account (a few minutes: the MCP servers start per test)
uv run inbox-to-order eval    # the documented demo outcomes, checked one by one
uv run pytest -m live         # optional: needs your ANTHROPIC_API_KEY and/or Gmail token; makes real calls
```

The browser test needs Chromium for Playwright (`uv run playwright install chromium`); without it, it is skipped.

| Area | Status |
|---|---|
| Full flow on the demo emails with the rule-based baseline, through both real MCP servers (stdio) | **Tested** (`pytest`, `eval`) |
| Review UI (React): board, read, review, confirm, order in the Demo ERP window | **Tested** in a headless browser (`tests/test_web_e2e.py`) and through the API (`tests/test_web_api.py`), including two conversations read at the same time |
| Demo ERP validation, idempotency, version conflicts, operator edits | **Tested** (unit and MCP tests) |
| Claude tool loop: tool limits, invalid outputs, refusals, network errors, cost accounting | **Tested with a scripted fake client.** This shows how the code reacts, not how the model behaves. |
| Gmail connector error cases (expired or revoked token, wrong scope, not configured) | **Tested against mocked Gmail API responses.** |

## Live: Gmail test account + Claude Haiku 5.5

2026-11-07, model `claude-haiku-5-5`, effort `medium`, through the UI (**Read all new**). The mailbox was a Gmail
account created for the test, holding 10 emails in 7 conversations, all written by the author with fictional
companies. One of them carried its purchase order as a **PDF attachment** (`PO-48217.pdf`, with a signature logo
in the email): the PDF's text was read through the Mail MCP server, by the app and by Claude, and the 4 order lines
of the proposal came from it. The logo was ignored.

| | Result |
|---|---|
| Time to read and sort all 10 emails, PDF included | **19.4 s**, up to 4 conversations at the same time |
| Same mailbox, one conversation at a time (2026-11-06) | 53.5 s |
| Time per email | 2.5 to 7.3 s for the last email read in each conversation (7 of the 10), almost all of it the model's reading. The email with the PDF took about 8 s (the audit log records whole seconds). |
| Estimated cost per email | about $0.001 to $0.002 (the same 7 emails) |
| Result | 3 ready to confirm, 1 needs a person, 3 set aside as not an order; the same in every run |

Times depend on the network and on Anthropic's load at that moment.

## Live: demo emails + Claude Haiku 5.5

`uv run inbox-to-order eval --extractor claude`, 2026-11-05, model `claude-haiku-5-5`, effort `medium`. Three
runs (the first two with an earlier version of the prompt) all passed the 60 checks. Last run, emails read one at
a time:

| Email | Outcome | Time | API calls | Tokens (in + out) | Estimated cost |
|---|---|---|---|---|---|
| A: new order, PDF + logo | proposal created | 10.6 s | 3 | 17,029 + 2,652 | $0.0030 |
| B: change in reply | new version | 5.5 s | 2 | 9,550 + 996 | $0.0015 |
| C: ambiguous item | proposal, question raised | 4.6 s | 2 | 8,637 + 910 | $0.0013 |
| D: hidden instructions | proposal, 2 passages ignored | 8.2 s | 3 | 15,577 + 2,159 | $0.0026 |
| E: duplicate | recognized, no model call | 0.02 s | 0 | n/a | n/a |
| F: scanned PDF | proposal, no lines, flagged | 4.1 s | 2 | 9,031 + 747 | $0.0013 |
| G: unknown sender, no PO | no proposal, reasons shown | 4.9 s | 2 | 8,769 + 900 | $0.0013 |
| H: change to an ERP order | new version against SO-0950 | 5.3 s | 3 | 13,659 + 596 | $0.0017 |
| **All 8** | **60/60 checks** | **50.0 s** | **17** | **82,252 + 8,960** | **$0.0127** |

Haiku 5.5 list prices: $0.10 / $0.50 per million input / output tokens. In all three runs the model made no tool
call outside its allowed scope, and nothing was written to the Demo ERP.

## What these results do not show

All the emails were written for the test. The results show that the flow works with a small model; they do not
measure accuracy on real customer emails, on hostile content written by someone trying hard, or with other Claude
models. See [SECURITY_AND_LIMITATIONS.md](SECURITY_AND_LIMITATIONS.md).
