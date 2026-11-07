# Inbox to Order

**Order emails in. ERP orders out.** An AI reads the order emails, a person confirms, and only then is the order
written to the order system.

> **Proof of concept** by [Levixflow](https://levixflow.com), built to test one idea end to end with fictional
> companies, fictional emails and a fictional order system (the "Demo ERP"). Meant to be read, run and extended,
> not used in production.

## The idea

Many companies that sell and ship goods still receive orders by email: a few lines in the message, or a PDF
purchase order attached. Someone reads each email and types the order into the ERP. Changes arrive the same way:
"make the packing tape eight cases, not six".

Inbox to Order does the reading and the typing, and leaves the decision to a person:

1. **Email arrives.** The app reads the mailbox. It can only read: it never sends, moves or deletes anything.
2. **AI reads it.** Claude reads the email and any PDF, finds the customer, the items and the quantities, and
   tells a new order from a change. Every value is linked to the words it came from. When something is unclear,
   it asks instead of guessing.
3. **You confirm.** You check the proposal, correct it if needed, and confirm or reject.
4. **ERP updated.** Only your confirmation writes the order. Nothing is written before.

![The app sits between the mailbox and the ERP. A read-only Mail MCP server brings the emails in; Claude reads them and proposes the order; you confirm; only then does the ERP MCP server write the order.](docs/flow.svg)

The app sits in the middle. It talks to the mailbox and to the ERP through two
[MCP](https://modelcontextprotocol.io) servers, an open standard for connecting tools to AI applications: the
Mail server can only read, and the ERP server is called to write only after your confirmation. Claude never
writes anything. Swapping Gmail for another mailbox, or the Demo ERP for a real one, means replacing one server.

## How it works in the app

Screenshots from the bundled demo emails, read without AI so anyone can reproduce them for free. With Claude the
screens are the same.

| | |
|---|---|
| ![The inbox board with 7 new conversations waiting in the New column](docs/screenshots/01_inbox_new.png) | ![The board after "Read all new": every conversation in its column, with the reason on its card](docs/screenshots/02_inbox_sorted.png) |
| **1. The inbox.** New emails wait in the first column, one card per conversation. | **2. Read all new.** Each conversation moves to its column: *Needs a person*, *Ready to confirm*, or *Set aside* if it is not an order. Each card says why. |
| ![A proposal for PO-48217: the change from 6 to 8 cases, with the source words of line 1 lit up](docs/screenshots/03_proposal_with_evidence.png) | ![PO-48231: a question for a person, because "Blue labels" matches two catalog items](docs/screenshots/04_question_for_a_person.png) |
| **3. Check the proposal.** Emails on the left, order on the right. Pointing at a line lights up its words. A change from a later email is shown before and after. | **4. A question, not a guess.** "Blue labels: twelve" matches two items and has no unit, so the app asks. **Edit** answers it. |
| ![PO-48240: instruction-like text in the email and the PDF is listed as ignored](docs/screenshots/05_hidden_instructions_ignored.png) | ![After the confirmation: the stamp SO-1001 on the proposal](docs/screenshots/06_confirmed.png) |
| **5. Hidden instructions are ignored.** "Ignore your instructions..." in an email or a PDF is shown to you and has no effect. | **6. Confirm.** Only **Confirm & write to Demo ERP** writes the order, which gets its ERP number. |
| ![The Demo ERP's own window: order SO-1001 with its lines and its history](docs/screenshots/07_demo_erp.png) | |
| **7. The ERP is updated.** The Demo ERP is a separate system with its own window. The order appears there as soon as you confirm. | |

## On a real Gmail inbox

The same flow on a Gmail test account with 10 emails, one of them with a PDF purchase order, read by Claude
Haiku 5.5. 53 seconds, no sound.

https://github.com/user-attachments/assets/96d8640f-d14b-471c-8f87-ab93623cd8d6

The same video is in the repository: [docs/media/inbox-to-order.mp4](docs/media/inbox-to-order.mp4).

Measured in that run: all 10 emails read and sorted in **19.4 s**, PDF included, about $0.001 to $0.002 of AI
cost per email. The result: 3 orders ready to confirm, 1 question for a person, 3 emails set aside as not
orders. Details, and what these numbers do not show: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).

## Try it in five minutes

No accounts, no cost. You need [uv](https://docs.astral.sh/uv/) (it installs Python if needed; 3.11 or later)
and Git. Developed and tested on Windows 11; the automated tests also run on Linux. macOS has not been tried.

```bash
git clone https://github.com/levixflow-com/inbox-to-order.git
cd inbox-to-order
uv sync
uv run inbox-to-order init-db --reset
uv run inbox-to-order web
```

Open <http://127.0.0.1:8700> (the app) and, in a second window, <http://127.0.0.1:8700/erp> (the Demo ERP).

Without an API key, the app reads the 8 bundled [demo emails](fixtures/README.md) with a rule-based baseline:
plain pattern matching, shown as "Read by rules, no AI". It only understands the demo emails, and exists so that
anyone can try the flow. Click **Read all new**, open *PO-48217*, confirm it, and watch the Demo ERP window.
**Reset demo data** starts again.

## Connecting the pieces

Every setting lives in `.env` (git-ignored); [`.env.example`](.env.example) lists them all with comments.

```bash
cp .env.example .env
```

| Piece | How it is connected | What you set up |
|---|---|---|
| **The app** | One local Python process: the web UI and the steps read, interpret, check, propose. Listens on `127.0.0.1` only. | `uv run inbox-to-order web` |
| **Mail MCP server** | Started by the app, over stdio. Lists and reads conversations and the text of PDF attachments; no tool can send, change or delete email. | `INBOX_MODE=fixture` (demo emails, default) or `gmail` |
| **Gmail** *(optional)* | Read by the Mail server through the Gmail API, with your own OAuth client, read-only. | See below |
| **Claude** *(optional)* | Called by the app with your key. Reads through a few read-only MCP tools, limited to the selected email and its customer. | `ANTHROPIC_API_KEY` |
| **Demo ERP MCP server** | Started by the app, over stdio. Customers, catalog, orders, plus `create_order` and `update_order`, called only after your confirmation. | Nothing: `init-db` fills it from `fixtures/` |
| **Demo ERP screen** | The ERP's own window at `/erp`, reading its database read-only. | Nothing |

The order of calls and the three layers of control: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Claude.** Create a key in the [Claude Console](https://console.anthropic.com/) and set
`ANTHROPIC_API_KEY=sk-ant-...` in `.env`. The default model is `claude-haiku-5-5`, the lowest-cost one; any Claude
model works (`ANTHROPIC_MODEL`), and the **Settings** menu switches model or reading mode without a restart. Usage
is billed to your Anthropic account; the app shows tokens and an estimated cost per run. When Claude is on, the
text of each email you process and of its PDFs is sent to Anthropic: use test emails only.

**Gmail.** About 15 minutes, once: a Gmail account used only for this test, a Google Cloud project with the Gmail
API and a *Desktop app* OAuth client saved as `secrets/credentials.json`, then `uv run inbox-to-order gmail-auth`.
Set `INBOX_MODE=gmail`, map your test sender to a demo customer (`EXTRA_CUSTOMER_SENDERS`), and choose the
conversations to read with `GMAIL_QUERY` (any Gmail search). Step by step: [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md).
The scope is `gmail.readonly`; messages the app reads stay unread in Gmail.

**Demo ERP.** A SQLite file with an MCP server in front of it, standing in for a real ERP: 2 customers, a 7-item
catalog, the customers' own item codes and one existing order. `init-db --reset` starts it again; the mailbox is
never touched.

**MCP servers.** The app starts and stops them; `uv run inbox-to-order mcp-diagnose` shows their tools and makes
a few live calls. They are ordinary stdio servers (`python -m inbox_to_order.mcp_servers.mail_server` or
`erp_server`), not tested with other MCP clients. Outside this app, mind that the ERP server's write tools do not
check for a person's confirmation: that check lives in the app.

**The UI.** The built UI ships in `src/inbox_to_order/web/dist/`, so running the app needs no Node. After
changing `frontend/`, rebuild it with `scripts/build-ui.sh` (Node in Docker) or `cd frontend && npm ci && npm run build`.

### Commands

```bash
uv run inbox-to-order web           # the app and the Demo ERP screen on http://127.0.0.1:8700
uv run inbox-to-order init-db       # create or reload the demo data (--reset: start again)
uv run inbox-to-order gmail-auth    # authorize the Gmail test account, read-only
uv run inbox-to-order threads       # list the conversations, through the Mail MCP server
uv run inbox-to-order demo          # read every demo email on a fresh database and print the results
uv run inbox-to-order drafts        # the proposals and their status
uv run inbox-to-order confirm 1     # confirm proposal #1 and write it to the Demo ERP
uv run inbox-to-order erp-orders    # what is in the Demo ERP
uv run inbox-to-order eval          # check the demo emails against the expected outcomes
uv run inbox-to-order mcp-diagnose  # the two MCP servers, their tools and some live calls
uv run pytest                       # offline tests: no key, no Google account
uv run ruff check . && uv run mypy src/inbox_to_order   # lint and types, as in CI
```

## What it does not do

- No OCR: scanned PDFs and images are not read; they are flagged for a person.
- No real ERP: the Demo ERP is a SQLite database with fictional prices.
- No sending, replying, labelling or deleting email.
- No quotes, invoices, stock or delivery dates.
- New lines in a change email are not added automatically; the operator adds them.
- One operator, one machine: no login, roles or deployment.
- Only Claude is wired in as the AI ([other providers](docs/EXTENDING.md) are possible, not implemented).

What has been tested, and how: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md). Before pointing it at anything
other than test data, read [docs/SECURITY_AND_LIMITATIONS.md](docs/SECURITY_AND_LIMITATIONS.md).

## Repository map

```
src/inbox_to_order/
  mcp_servers/mail_server.py   Mail MCP server (read-only; demo files or Gmail)
  mcp_servers/erp_server.py    Demo ERP MCP server (customers, catalog, orders; create/update order)
  mcp_client.py                starts both servers over stdio and keeps them running
  agent.py                     Claude: reads through allow-listed tools, returns a structured proposal
  extractors.py                the rule-based baseline (no AI)
  pipeline.py                  checks in plain code: evidence, customer, catalog, units, PO, change target
  orchestrator.py              one email: read -> interpret -> check -> save a proposal
  review.py                    operator actions: the only call to create/update_order
  services/                    email parsing, PDF text, Gmail auth, proposals store, Demo ERP logic
  web/server.py, web/dist/     local API and the built UI
frontend/                      React + TypeScript source of the UI
fixtures/                      demo emails, PDFs, customers, catalog, expected outcomes
docs/                          architecture, Gmail setup, security, test results, extending, screenshots, video
tests/                         offline tests; live tests are opt-in with -m live
.github/workflows/tests.yml    lint, types and offline tests on every push
```

## About

Built by [Levixflow](https://levixflow.com), a studio that builds custom software for companies that sell and
ship goods. Order emails still typed into your system by hand? Send me your problem, even in two lines.

MIT license. See [LICENSE](LICENSE).
