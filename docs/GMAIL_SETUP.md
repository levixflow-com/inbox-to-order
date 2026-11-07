# Optional: connect your own Gmail test account (read-only)

The demo emails need none of this. Use this guide when you want the app to read order emails from a
**dedicated Gmail test account you own**. It takes about 15 minutes, once.

> **Status of this guide.** The author followed these steps in November 2026 with a new Gmail test account and a
> "Desktop app" OAuth client in Testing status, then read 10 test emails with Claude Haiku 5.5 through the app
> (results in [TEST_RESULTS.md](TEST_RESULTS.md)). Error cases (expired or revoked token, wrong scope, not configured) are covered by
> automated tests against *mocked* Gmail API responses. Google Cloud console labels change over time; if a screen
> looks different, follow Google's current documentation:
> <https://developers.google.com/workspace/gmail/api/quickstart/python> and
> <https://developers.google.com/workspace/gmail/api/auth/scopes>.

## What access is requested

- One scope: `https://www.googleapis.com/auth/gmail.readonly`.
- Google classifies `gmail.readonly` as a **restricted** scope. It lets the app read all messages and
  attachments in the mailbox, which is why you should use a dedicated test account with only fictional
  demo emails in it.
- The code only calls `users.threads.list` (with `q=` your `GMAIL_QUERY`), `users.threads.get` and
  `users.messages.get` (raw format). There is no code that sends, drafts, labels, modifies or deletes email, and
  the requested scope would not allow it. The query limits what the app *lists*; the scope itself still allows
  reading the whole mailbox, which is why the mailbox should be a test account.
- Messages the app reads stay unread in Gmail: marking them read would need a wider scope, not requested on purpose.
- The OAuth client is **yours** (a "Desktop app" client in your own Google Cloud project). Nothing goes through
  a third-party server. Tokens stay on your machine in `secrets/token.json` (git-ignored, file mode 600 where the OS
  supports it).
- If you also use Claude, the text of each email you process and its PDF text are sent to Anthropic with your
  API key. The UI asks you to acknowledge this before processing Gmail messages with Claude.

## 1. Prepare a test mailbox

Create a Gmail account used only for this experiment, and put only test emails in it. Use a second address
of yours as the "customer" sender.

Decide which conversations the app should read. It lists the conversations matching `GMAIL_QUERY` (any Gmail
search, at most 25):

- **A label** (the default, `label:orders-demo`): create the label **`orders-demo`** in the test account and apply
  it by hand, or with a filter (*Settings → Filters and blocked addresses → Create a new filter*, `from:` your
  customer address, action *Apply the label: orders-demo*).
- **The whole inbox** of the test account: `GMAIL_QUERY=in:inbox -from:accounts.google.com`. The second part
  leaves out Google's own security emails (for example the alert sent when you authorize the app), so they are
  not shown on the board or sent to Anthropic.

## 2. Create the OAuth client

In the [Google Cloud console](https://console.cloud.google.com/):

1. Create a new project (for example `inbox-to-order-test`).
2. **APIs & Services → Library**: enable the **Gmail API**.
3. **Google Auth Platform** (formerly "OAuth consent screen"):
   - *Branding*: app name and support email.
   - *Audience*: user type **External**, publishing status **Testing**; add the test mailbox address under
     **Test users**.
   - *Data access*: add the scope `.../auth/gmail.readonly`.
4. **Clients → Create client → Application type: Desktop app**. Download the JSON file.
5. Save it as `secrets/credentials.json` in the repository folder (the `secrets/` folder is git-ignored).

Because the app stays in **Testing** status with you as the only test user, Google does not require app
verification, but you will see an "unverified app" warning during consent. Publishing an app that uses a
restricted scope to other users requires Google's verification and a security assessment; that is out of scope.

## 3. Authorize

```bash
uv run inbox-to-order gmail-auth
```

A browser window opens on a `localhost` redirect. Sign in with the **test mailbox**, accept the read-only
access, and the token is written to `secrets/token.json`. The app refuses to use a token that does not include
`gmail.readonly`.

**Token lifetime:** for apps in Testing status, Google issues refresh tokens for Gmail scopes that expire after
**7 days**. When that happens the app shows "The Gmail authorization expired or was revoked…" and asks you to
run `uv run inbox-to-order gmail-auth` again. It never falls back to fixture data while in Gmail mode.

## 4. Tell the app who your test customer is

The customer is always resolved from the sender address. Map your "customer" sender address to a demo
customer in `.env`:

```ini
INBOX_MODE=gmail
EXTRA_CUSTOMER_SENDERS=PINE-RIDGE=my.second.address@gmail.com
```

then reload the Demo ERP's customer records (keeps existing proposals and orders; add `--reset` for a clean start):

```bash
uv run inbox-to-order init-db
```

Set `GMAIL_QUERY` as chosen in step 1 if it is not the default `label:orders-demo`.

## 5. Send test emails and run

From the "customer" address (mapped to `PINE-RIDGE`), send the test account:

1. **A new order.** Subject `PO-48217 - next week's delivery`, body as in `fixtures/messages/A_initial_order.eml`
   ("Please ship to our Columbus receiving dock..."), attachment `fixtures/attachments/PO-48217.pdf`. A signature
   logo is fine: images are ignored.
2. **A change, as a reply** in the same conversation: "Small change to PO-48217: make the packing tape eight cases,
   not six."
3. Optionally, from an address mapped to `LAKESHORE`: "Please change the stretch film from 10 rolls to 12 rolls
   on PO-48190." — a change to an order that exists only in the Demo ERP.

Then:

```bash
uv run inbox-to-order threads     # lists the matching conversations, through the Mail MCP server
uv run inbox-to-order web
```

Open <http://127.0.0.1:8700>. The header says "Gmail test inbox, read-only" (with `INBOX_MODE=gmail`; the
**Settings** menu also switches between the demo emails and Gmail). Before the first read, the app asks you to
accept that the text of each email and its PDFs is sent to Anthropic. Then click **Read all new**.

With Claude configured, the model reads the messages through the same MCP tools as with the demo emails.
Without a key, the rule-based baseline only understands phrasing close to the demo emails, so write the test
emails as above.

**Starting again.** `uv run inbox-to-order init-db --reset` deletes the app's proposals and the Demo ERP's
orders. The mailbox is not touched: every message simply shows as not read yet again. (The **Reset demo data**
button is shown only with the demo emails.)

Optional live check (lists up to 5 threads from the authorized mailbox, reads one):

```bash
uv run pytest -m live -k gmail
```

## Revoking access

Delete `secrets/token.json`, and remove the app at <https://myaccount.google.com/permissions> for the test
account. Deleting the Google Cloud project removes the OAuth client.

## Troubleshooting

| Message | Meaning |
|---|---|
| `GMAIL_NOT_CONFIGURED: Gmail is not configured: OAuth client file not found…` | `secrets/credentials.json` is missing. Fixture mode still works. |
| `Gmail is configured but not authorized yet` | Run `uv run inbox-to-order gmail-auth`. |
| `The Gmail authorization expired or was revoked…` | 7-day Testing-status expiry or manual revocation. Run `gmail-auth` again. |
| `The stored token does not include gmail.readonly` | The token was created for another scope. Run `gmail-auth` again. |
| *Sender ... is not a known customer in the Demo ERP* | Add the address to `EXTRA_CUSTOMER_SENDERS` and run `init-db`. |
| *No order conversations found* | Nothing matches `GMAIL_QUERY` (default `label:orders-demo`). Try the same search in Gmail. |
