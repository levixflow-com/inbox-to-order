# Extending the proof of concept

The project is small on purpose. This page lists the realistic next steps, where each one plugs in, and what
it does not solve by itself.

## Connect a real order system (ERP)

**Where:** write a new MCP server that exposes the same tools as `mcp_servers/erp_server.py`
(`find_customer`, `get_customer`, `find_product`, `list_products`, `get_order`, `list_orders`, `create_order`,
`update_order`) with the same input and output models (`models.py`). Point `SERVER_MODULES["erp"]` in
`mcp_client.py` at it. The orchestrator, the checks, the review UI and the tests of the app side stay the same.

**What it does not solve:** the mapping. Your ERP has its own customer ids, item codes, units of measure, price
lists, order types, credit checks and approval rules. The new server has to translate between those and this
contract, and decide which of the ERP's validations to surface as clear error codes. MCP makes that work
self-contained; it does not do it for you. Keep the write tools idempotent and version-checked, as the Demo ERP
is (`services/erp.py` is a compact reference).

**Practical advice:** start read-only (customers, catalog, orders) and keep `create_order` / `update_order`
pointing to a sandbox company or a staging environment until the mapping is proven on real emails.

## Add a CRM or another system

Add a third server (for example `crm`) to `SERVER_MODULES`; its tools are discovered automatically. Decide for
each tool whether the model may read it (add it to `MODEL_ALLOWED_TOOLS` and extend `gate_tool_call` with a
scope rule) or whether only the app calls it. Tool names must be unique across servers.

## Read another mailbox

`services/inbox_sources.py` has two sources behind the same interface: `FixtureInbox` and `GmailInbox`. An IMAP
or Microsoft Graph source would implement `list_threads`, `get_thread` and `get_attachment` and be selected by
`INBOX_MODE`. Keep it read-only and keep the label/folder filter. Threading, HTML-to-text and the content hash
are already shared code (`services/email_parser.py`).

## Use another Claude model, or another provider

- **Another Claude model:** set `ANTHROPIC_MODEL` (or use the UI's Settings menu). Add its list price to
  `MODEL_PRICES_USD_PER_MTOK` in `config.py` to see cost estimates. Run `uv run inbox-to-order eval --extractor
  claude` to compare models on the demo emails.
- **Another provider:** implement the `Extractor` protocol in `extractors.py` (`name` and
  `async extract(ctx, box) -> Extraction`). `agent.py` shows the pattern: offer the allow-listed MCP tools
  (`anthropic_tool_definitions` converts them; other APIs need a similar converter), gate every call with
  `gate_tool_call`, and return a validated `Extraction`. Everything after the extractor is provider-neutral.

## Read scanned PDFs (OCR)

Out of scope here. The natural place is `services/pdf_extract.py`, which today returns `no_text` for scanned
pages. An OCR step changes the evidence rule: excerpts would point to OCR output, not to text in the PDF, and OCR
errors (for example `8` read as `B`) need their own review signal.

## Run it for more than one person

Needed before any shared use: authentication on the UI, roles (who may confirm), a server database instead of
SQLite, secrets in a vault, and a decision about where the model calls run. The confirmation step should stay a
human action in the app, not a tool the model can call.

## Measure before scaling

`fixtures/expected_outcomes.json` and `uv run inbox-to-order eval` are the starting point for an evaluation set.
Add anonymised or synthetic emails that look like your real ones, record the expected proposal for each, and
compare extractors and models on accuracy, questions raised, time and cost before changing anything else.
