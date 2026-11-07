# Security and limitations

Inbox to Order is a **proof of concept**. It runs on fictional data against a fictional order system. It has no
production guarantees. This page lists what the code enforces, what it does not, and what is untested.

## What is enforced in code

**Email is read-only.**
- No MCP tool, function or dependency call in this repository sends, drafts, labels, modifies or deletes email.
  The Gmail connector uses `threads.list`, `threads.get` and `messages.get` only, under `gmail.readonly`
  (tests: `tests/test_gmail_connector.py`, including a check that the Gmail class has no write methods).
  A consequence: messages the app has read stay unread in Gmail. What has been read is shown on the app's
  board; marking messages read would need the `gmail.modify` scope, which is not requested on purpose.
- In Gmail mode only conversations matching `GMAIL_QUERY` (default `label:orders-demo`) are listed.

**The model proposes, code checks, a person confirms.**
- The model sees five read-only MCP tools. The ERP write tools (`create_order`, `update_order`), the mailbox
  listing and the order listing are not offered to it.
- Every model tool call passes `gate_tool_call`: only the selected conversation, only attachments of the selected
  message, only the actual sender, only the customer resolved from the sender. Refusals are audited
  (`tests/test_prompt_injection_boundary.py` drives a scripted hostile model through 7 refused calls, including
  an attempt to call `create_order`, and checks the Demo ERP is unchanged).
- Values are accepted only with a verbatim excerpt from the cited source. Customer, item, unit, quantity, PO and
  address are resolved by deterministic code; ambiguous cases become questions. Units are never converted.
- Processing an email writes only the app's own state. The Demo ERP is written only by `review.confirm_and_write`,
  after the operator's click, for a proposal with no open question, at the version the operator saw
  (`tests/test_idempotence.py` checks that a full run over all demo emails leaves the Demo ERP unchanged).
- The Demo ERP validates every write again (customer, address ownership, SKU, selling unit, quantity range,
  duplicate lines, unique PO per customer), is idempotent per key and refuses stale versions.

**Untrusted content stays data.**
- Email bodies, subjects, PDF text and tool results are treated as untrusted. The system prompt says so, and
  instruction-like passages are detected by a simple pattern list and shown to the operator as "ignored". The
  pattern list is a visibility aid, not the defence: the defence is the tool scope, the checks and the operator.
- HTML email is converted to plain text (scripts, styles and quoted history dropped) and never rendered as HTML.
- Images (for example signature logos) are not read and not sent to the model. Other non-PDF attachments are
  not read and are flagged for the operator.

**Bounded inputs.** At most 25 threads listed, 10 messages per thread, attachments up to 5 MB, 20 PDF pages,
40 000 characters of PDF text, 20 000 characters of body, 8 model tool rounds, quantities up to 10 000.

**Idempotency and integrity.** Each source message is processed once; content duplicates are detected; proposal
changes use optimistic versions; each save is one SQLite transaction. A failed model call or invalid output saves
nothing (`tests/test_agent_failures.py`). ERP writes use the key `proposal-<id>-v<version>`, so a retried
confirmation never creates a second order.

**Reading several conversations at once.** "Read all new" reads up to 4 conversations at the same time. The
server refuses a second read of a conversation that is already being read; the emails of one conversation are
read one after another. Each run logs its tool calls under its own conversation (`tests/test_web_api.py`), and the
Mail server serializes its Gmail calls (the Gmail client is not thread-safe). Two proposals for the same customer
and PO cannot both be saved: the save runs in one write transaction against a unique key.

**Secrets.**
- `.env`, `secrets/`, `credentials.json`, `token.json`, `client_secret*.json`, `data/` and `*.db` are git-ignored.
- The Anthropic API key is not passed to the MCP server processes.
- The audit log keeps an allowlist of keys with short values; message bodies and tokens are not logged.
- The UI binds to `127.0.0.1`.
- The React UI talks to a local API. Any website open in the same browser could try to call it, so every
  API request must carry a random token created when the server starts and placed only in the pages it
  serves (other sites cannot read them). The server also refuses unknown `Host` headers (DNS rebinding) and
  foreign `Origin` headers, and every write needs the proposal version the page showed. Tested in
  `tests/test_web_api.py`.
- Email and PDF text is rendered as text by React, never as HTML. Fonts are bundled; the pages load nothing
  from other sites.

## Data sharing you should know about

- **Anthropic:** when Claude is used, the selected email's text and its PDF text, plus customer, catalog and order
  data returned by the read-only tools, are sent to Anthropic under your API key and Anthropic's terms. The UI asks
  for an explicit acknowledgment before processing Gmail messages with Claude. The rule-based baseline sends
  nothing anywhere.
- **Google:** in Gmail mode the app reads from the Gmail API with your own OAuth client. `gmail.readonly` is a
  **restricted** scope that can read the whole mailbox even though the app lists only what `GMAIL_QUERY` selects;
  use a dedicated test account. Google's own security emails can be left out of the query
  (`-from:accounts.google.com`), so they are not sent to Anthropic.

## Known limitations

- **No OCR.** Scanned PDFs are reported as "no extractable text"; the operator enters the lines.
- **Text PDFs with simple layouts.** pypdf's text order can differ from the visual layout of complex tables.
- **Rule-based baseline** is tuned to the demo emails. It exists to run the flow and the tests without a key.
- **Prompt-injection detection** is pattern-based and will miss rephrased attacks. The model may still be
  influenced in what it *proposes*; the checks, the absence of dangerous tools and the operator limit the impact.
- **Changes by email** can change quantities and the ship-to of existing lines. Added or removed lines are
  flagged for the operator, who can edit them; they are not applied automatically.
- **Customer identification** relies on the sender address or domain. Spoofed senders are not detected
  (no SPF/DKIM evaluation).
- **Single user, local.** No login on the UI (bound to localhost), no roles, SQLite only.
- **Prices** in the Demo ERP are fictional.

## What has not been tested

- **Real customer emails.** Claude Haiku 5.5 passed all 60 checks on the 8 demo emails in three live runs
  (2026-11-05), and read a Gmail test account with 10 test emails (2026-11-06 and 2026-11-07; see [TEST_RESULTS.md](TEST_RESULTS.md)).
  All those emails were written for the test. Accuracy on real, messier emails, on hostile content written by
  someone trying hard, and with other Claude models has not been measured.
- **Gmail beyond one test account.** The OAuth flow and live reading were run by the author on one Gmail test
  account in Testing status. Token expiry, revocation and wrong scopes are tested only against mocked Gmail API
  responses. Google Workspace accounts and verified (published) apps were not tried.
- **Other operating systems.** Developed and tested on Windows 11; Linux and macOS were not run.

## Reporting a problem

This is a personal demonstration project. Do not put real customer data or credentials in issues.
