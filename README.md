# Receipt POC

An AI-powered receipt and expense tracker that turns Telegram receipt photos into organized records, with learned vendor categories, spending reports, CSV exports, and a private web dashboard.

## What it does

1. Receives Telegram images through a verified webhook.
2. Compresses and normalizes images before Vision processing.
3. Extracts strict receipt JSON using OpenAI Vision.
4. Rejects repeated image bytes per user before extraction, alongside the database-enforced vendor/date/total rule.
5. Remembers vendor categories and pauses for unknown vendors.
6. Stores receipt images in a private Supabase Storage bucket.
7. Sends a Markdown confirmation with a short-lived signed link.

## Required environment variables

Create a local `.env` (ignored by Git) and configure:

```env
DATABASE_URL=postgresql+psycopg://...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_RECEIPTS_BUCKET=receipts
SIGNED_URL_TTL_SECONDS=900
```

Use the Supabase Session Pooler URI if your network does not support IPv6.
The application uses psycopg 3: `DATABASE_URL` must start with
`postgresql+psycopg://`, not bare `postgresql://` (which selects psycopg2).

## Local checks

```sh
python -m pytest -q
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run these commands from the repository root with the virtual environment active.
The full suite includes the existing tests under both `tests/` and `app/`.
OpenAI, Telegram and Storage calls are faked; tests do not use the application's
`DATABASE_URL` or read `.env` to connect to a database.

Repository tests run on SQLite with foreign keys enabled. To also run the real
PostgreSQL migration, ownership constraints and concurrency tests, create an empty,
disposable database named **`receipt_poc_test`** and set `TEST_DATABASE_URL`:

```sh
TEST_DATABASE_URL='postgresql+psycopg://localhost/receipt_poc_test' python -m pytest -q
```

Use local test credentials as needed. Each PostgreSQL test creates and removes a
random schema in that database; never point this setting at Supabase or a database
containing real data. PostgreSQL tests skip explicitly if the variable is absent.
On minimal PostgreSQL distributions without `pgcrypto`, the migration fixture
uses the built-in `pg_catalog.gen_random_uuid()` and omits only the baseline's
extension-install statement. All table DDL and migration 002 run unchanged.

## User ownership (Phase 2)

One Telegram chat is one account, created automatically on its first processed
photo or text message. Group chats share an account; individual sender identities
inside a group are not separate users.

- `users` maps a unique `telegram_chat_id` to a UUID.
- Receipts and pending conversations have a required `user_id`. Foreign keys
  enforce matching user/chat pairs and matching pending/receipt ownership.
- Duplicate detection uses `(user_id, vendor_normalized, receipt_date, total_amount)`.
  The legacy chat-based unique constraint is retained and remains equivalent
  while each user has exactly one chat.
- `user_vendor_memory` is keyed by `(user_id, normalized_name)`. Two users can
  assign different categories to the same vendor. Runtime code never reads or
  writes the legacy global `vendor_memory` table.
- Existing Telegram chat IDs, receipt IDs, webhook history and storage object
  paths remain unchanged. New images still use `chat_id/event_id.jpg`.
- New account/memory tables have RLS enabled with no public policies. The backend
  needs a privileged database connection; these tables are not browser APIs.

## Apply migration 002 to an existing deployment

Migration 002 is a one-time, transactional migration after `001_init.sql`.
Do not rerun 001 against an existing database, use `create_all` as a migration,
or run migration tests against the deployed database.

1. Back up the database and compare the deployed tables/constraints with
   `migrations/001_init.sql`. The SQL migration aborts if a receipt's chat differs
   from its webhook event, or a pending conversation's chat differs from its receipt.
2. Pause receipt processing and drain in-flight tasks. Keep it paused until both
   the migration and updated application are deployed: the old application cannot
   insert the newly required `user_id` fields.
3. Apply `migrations/002_user_scoping.sql` with the table-owning migration role,
   using the Supabase SQL editor or `psql` with `ON_ERROR_STOP=1`. Keep the supplied
   `BEGIN`/`COMMIT` transaction. Locks time out after 5 seconds and statements after
   60 seconds; investigate failures before retrying rather than bypassing checks.
4. Verify receipt/pending counts and image paths are unchanged, and that all rows
   have an owner matching their chat. Deploy the updated backend, then resume
   processing. Verify two private chats can record the same receipt and learn
   different categories; verify an existing pending receipt can be completed.

The migration creates users from all historical chat IDs and backfills ownership.
Vendor memory is seeded only from each user's own **completed** receipts when that
vendor has exactly one distinct nonblank category. Ambiguous categories, pending
receipts and global-only vendor entries are not copied; subsequent receipts ask
for a category. Historical categories may themselves have come from global memory;
the original data cannot establish who first chose them. Existing receipts are
preserved, not reclassified.

No tables, columns or legacy rows are dropped. Backfill updates and unique-index
creation take locks and may need a larger maintenance window on a large database.
A migration error rolls back all changes. After a successful migration, do not
roll back to the old application alone: keep processing paused and roll forward
with a fix, or perform a coordinated backup restore with explicit approval.

Phase 2 keeps the existing confidence and text-routing behavior. Medium confidence
still follows the normal path, and a short text reply can still resolve a pending
category. Review states and controlled conversational routing belong to later phases.

## Deploying

The included Dockerfile runs the web service on `0.0.0.0:$PORT`. Configure the same environment variables on the host. Do not upload or commit `.env`.

After deployment, configure Telegram with:

```text
https://YOUR_PUBLIC_HOST/webhooks/telegram
```

using the exact value of `TELEGRAM_WEBHOOK_SECRET` as Telegram's webhook secret token.

### Repeat-image protection

Saved image SHA-256 values are checked per user before Vision runs and again
under an owner-row lock when saving to PostgreSQL. This prevents identical
image bytes from creating another receipt when extraction returns different
amounts, including while the first receipt awaits a category. Unsaved low-confidence
images can still be retried. No additional migration is required.

Recompressed images and new photos can have different hashes and still rely on
the vendor/date/total rule. Existing duplicate records are not changed. Vision
now uses high image detail (higher image-token usage) and explicit instructions
to copy the printed final total; this does not guarantee extraction accuracy.

## Phase 3: receipt lifecycle

Lifecycle values live in `app/statuses.py`. Receipt statuses now support
`PROCESSING`, `PENDING_CATEGORY`, `NEEDS_REVIEW`, `COMPLETED`, `DUPLICATE`, and
`FAILED`. The existing Telegram flow still saves a valid receipt as
`PENDING_CATEGORY` or `COMPLETED`; review prompts belong to Phase 4.

Before valid data exists, the webhook event tracks processing, duplicate rejection,
retry requests and failures. No placeholder expense is inserted for an unreadable
image or duplicate. Events retain their first `processing_started_at`, latest
`updated_at`, terminal `completed_at`, and safe `error_code`. For an event,
`completed_at` means the attempt ended (including failed/retry/duplicate outcomes).

Saved receipts gain `processing_started_at`, `updated_at`, `completed_at`,
`review_reason`, and `failure_reason`. Receipt `completed_at` means successful
completion, including category assignment. A Telegram delivery failure marks the
event failed without undoing the saved expense or changing its completion status.
Reason fields support later review workflows; Phase 3 does not change confidence
thresholds or introduce correction commands. Application writes maintain timestamps.

### Apply the Phase 3 migration before deploying

1. Run the full test suite against a disposable `receipt_poc_test` PostgreSQL
   database using `TEST_DATABASE_URL` (never the production database).
2. Pause receipt processing and take a database backup.
3. Run `migrations/003_receipt_lifecycle.sql` once, after migrations 001 and 002.
   Keep its transaction intact. It adds columns and expands the receipt status
   check, with 5-second lock and 60-second statement timeouts. No receipts,
   categories, owners, images, or duplicate constraints are removed.
4. Verify existing receipt counts, amounts and categories are unchanged. Then
   push/deploy the Phase 3 backend and resume processing. Do not deploy this
   backend before the migration; the ORM expects the new columns.

Historical timestamps use the earliest processing attempt, resolved category time,
or last recorded event update as available evidence, not exact reconstructed times.
Unknown historical start/completion times remain NULL. The migration is one-time;
a repeat attempt fails and rolls back. On failure, inspect the error before retrying.

Smoke check with a new receipt, category reply, repeated image, and unreadable
photo. Check receipt/event statuses and timestamps in Supabase. No new Telegram
commands are expected in this phase.

## Phase 4: human review

High-confidence extractions with no validation concerns follow the usual category
and completion path. Medium confidence saves a `NEEDS_REVIEW` draft. Low confidence
and invalid required fields request a clearer photo without saving an expense.

The extractor now also returns `Date_Text`, the printed date substring before
normalization. Deterministic checks flag ambiguous numeric dates (both day/month
orders are possible), missing/unverifiable date evidence, future dates, and VAT
above the total, even when confidence is High. Future dates use the current date
in UTC+08:00 (Manila). Unknown date formats require an explicit date rather than
silently choosing an interpretation. These checks still depend on the model
reading the printed characters correctly; High confidence is not a guarantee.

During review, Telegram displays vendor, date with a month name, printed date,
total, VAT, category, reasons and a private signed image link. Commands are:

- `CONFIRM`: accept the displayed values when the date is unambiguous.
- `CONFIRM YYYY-MM-DD`: explicitly choose/correct the date and confirm the other
  displayed values. For Keigo's `9/10/26`, use `CONFIRM 2026-09-10`.
- `REVIEW`: display the pending review again (also works after a restart).
- `RETRY`: explicitly discard only the caller's unconfirmed review draft, then
  send a clearer photo. This cannot delete a completed receipt. The original
  webhook/attempt history and private uploaded image remain; orphan-image cleanup
  is not part of this phase.

Ambiguous/unverified/future dates require the explicit ISO-date command. A future
confirmed date or VAT above total cannot be accepted. Other field corrections
remain Phase 5; use RETRY when those fields are wrong. Any corrected date is checked
against the existing per-user duplicate rule in the same transaction.

For unknown vendors, review happens before the category question. Category memory
is learned only after confirmation and category assignment. Review commands cannot
be mistaken for category names. One open category/review conversation per user is
preserved; new photos wait until it is resolved, and repeated images still get
normal duplicate detection. Receipt, pending conversation, and event status are
saved together. Concurrent saves/confirmations lock the owner row in PostgreSQL.

### Phase 4 deployment order

Validate against a disposable PostgreSQL `receipt_poc_test` database first. Then
back up the live database and pause receipt processing. Apply
`migrations/004_receipt_review.sql` **once**, after migration 003. On an existing
project, do not rerun migrations 001, 002, or 003. Migration 004 adds nullable
`receipts.raw_date_text` and expands the webhook status check; it preserves all
existing receipts, categories, pending conversations, and duplicate constraints.
Historical date text remains NULL; old receipts are not reprocessed or reclassified.
The migration takes table locks (5-second lock/60-second statement timeouts) and
rolls back on error. Deploy this backend only after the migration succeeds.

Smoke-test a readable ambiguous-date receipt: it must pause for an explicit date,
then request a category only if the vendor is unknown. Verify `NEEDS_REVIEW` has no
completion timestamp, confirmation completes or changes to `PENDING_CATEGORY`, and
category assignment completes the receipt. Check a duplicate image and low-confidence
image too. Automated tests use fake Vision/Telegram/Storage clients; no live API
calls are made. PostgreSQL migration and concurrency tests require TEST_DATABASE_URL.


## Phase 5: correct a saved receipt in Telegram

Send `/receipts` to list your latest 10 completed receipts and their full IDs.
Copy the ID of the receipt you want to change into one of these commands:

```text
/edit <receipt-id> total 450.00
/edit <receipt-id> date 2026-09-10
/edit <receipt-id> vendor Starbucks
/edit <receipt-id> category Transportation
```

Replace `<receipt-id>` with the complete ID shown by the bot. Send `/help` for
examples. Dates accept YYYY-MM-DD or explicit DD/MM/YYYY (22/09/2026); short
ambiguous dates such as 9/10/26 are rejected. Totals must be positive, no more
than two decimal places, and at least the recorded VAT. Future dates are rejected.
Vendor/category values are limited to 200/100 characters.

Commands target only receipts belonging to the Telegram chat. Corrections lock the
owner and check the existing duplicate rule before committing. They update
`updated_at`, preserve `completed_at`, the image and extraction evidence, and send
a confirmation with the saved values. The original extraction confidence is
preserved; this is not an AI re-extraction. Vendor and category corrections affect
only this receipt, leaving learned categories for future receipts unchanged.

Finish any review/category flow for the target receipt first. During review,
`CONFIRM YYYY-MM-DD` still handles date confirmation and `RETRY` discards the
unconfirmed draft. Slash commands, including malformed ones, cannot become
category names. Other completed receipts can be edited while a draft is pending.

No new migration is needed after migration 004. Run the tests (including the
disposable PostgreSQL suite) before deploying this backend through Render. Smoke
test `/receipts`, correct one saved receipt, verify the response and `updated_at`,
and restore the original value if testing with real data. Do not rerun migrations
001–004. Correction audit history is reserved for the later audit phase.


## Phase 6: expense queries

Telegram supports these deterministic, read-only commands:

```text
/summary 2026-09
/categories 2026-09
/category 2026-09 Dining
/largest
/largest 2026-09
/search Keigo
/receipt <receipt-id>
/compare 2026-08 2026-09
```

`/summary` returns a receipt count and total. `/categories` groups by stored
category, and `/category` filters by a case-insensitive exact category name
(including names containing spaces). `/largest` returns up to five receipts,
across all dates unless a month is supplied. `/search` matches a literal,
case-insensitive vendor substring and returns up to ten matches ordered by
receipt date, newest first. `%` and `_` are literal characters, not wildcards.
`/receipt` retrieves one owned completed receipt by its full ID.

All commands use **completed receipts only**, scoped to the Telegram chat's user.
The reporting date is `receipt_date`, not upload or completion time. Month ranges
include the first day and exclude the next month's first day. Totals come from
SQL SUM/COUNT over stored decimal amounts; no model calculates them. The current
schema has no currency field, so totals assume receipts use the same currency;
there is no currency conversion. Empty months return zero. Month comparison is
second month minus first; percentage change is N/A when the first total is zero.
These are totals of recorded receipts, not a claim to include all spending.

The `ExpenseQueries` service in `app/expenses.py` exposes search, single-receipt,
monthly/category summary, largest-expense and comparison tools without Telegram
or an LLM. Search supports optional inclusive start / exclusive end date filters,
category/vendor filters, limit (1–50) and offset for pagination. Ranking ties use
receipt date then ID for stable ordering. Queries reflect corrected fields
immediately. Existing validated correction functions remain the write tools.

Query commands never resolve pending category/review conversations. Natural-language
questions are reserved for Phase 7; use the explicit commands above for now.
No schema migration or new environment variable is required. Validate with the
full disposable PostgreSQL suite, push the phase commit, and deploy the latest
commit on Render. Smoke-test `/summary 2026-09` and `/categories 2026-09`, checking
against the completed receipt dates/amounts in `/receipts`. Do not rerun migrations.


## Phase 7: natural-language expense questions

After deployment, ask the bot directly:

- How much did I spend this month?
- How much did I spend on Dining in September?
- What are my five biggest expenses?
- Compare this month with last month.
- Find my Keigo receipt.
- Show Transportation expenses this week.

The existing OPENAI_API_KEY and OPENAI_MODEL settings are reused (default gpt-4o).
There is no migration or new required configuration. Question text and the current
Asia/Manila date are sent to OpenAI using a strict Responses function tool. The
model selects one read-only query; it receives no database receipts, credentials,
chat ID, or owner ID. The backend validates every argument and supplies the owner
from the Telegram chat. No model-selected SQL, writes, or arbitrary functions are
allowed. Output is formatted directly from the existing SQL tools, without a second
model call or model-written totals. Response storage is disabled for these requests.
See the [official function-calling guide](https://developers.openai.com/api/docs/guides/function-calling).

**Category replies now use an explicit prefix:** when asked for a category, send
`CATEGORY Dining`, replacing Dining with your chosen category. This prevents a
question or greeting from becoming a learned category. Bare text (including a bare
category name) goes to the read-only assistant, leaving the pending receipt open.
Existing CONFIRM, CONFIRM YYYY-MM-DD, REVIEW and RETRY review commands retain their
behavior. Slash commands still bypass the model. Questions can be asked while a
receipt is pending; they do not confirm it or change its category. Corrections
remain explicit `/edit` commands.

Questions are independent; there is no conversation history or implicit “that
receipt” selection. This/last month use the current Manila calendar date; a named
month without a year assumes the current year. Search date ranges include start
and exclude end, with weeks beginning Monday. Answers display the selected period
and filters so an interpretation can be checked. Category filters are exact names,
not inferred aliases. Unsupported or ambiguous questions receive a fixed request
to clarify. Monthly totals, category summaries, comparison and limited receipt
lists are supported; other summary periods require a more specific supported
question. Money retains the Phase 6 single-currency assumption.

Question input is limited to 2,000 characters, output to one validated tool call,
and receipt lists to ten results. Production requests have a 20-second SDK timeout
and no automatic SDK retries. Provider/validation failures show a safe retry/help
message without touching pending receipts. Tests fake OpenAI, Telegram and storage;
they validate routing and tool execution, not live model interpretation quality.

Before pushing, run the full disposable PostgreSQL suite. After Render deploys,
ask “How much did I spend in September 2026?” and compare with `/summary 2026-09`.
Test a question while a receipt awaits a category, then resolve it with CATEGORY.
No Supabase migrations should be rerun.


## Phase 8: receipt audit history

Use `/history <receipt-id>` (copy an ID from `/receipts`) to see the newest five
history entries. Use `/history <receipt-id> 2` for the next page. History is scoped
to the Telegram chat owner and can be viewed without resolving pending receipts.
Times are displayed in UTC. History is not sent to the conversational model.

The new `receipt_events` table records CREATED, REVIEW_CONFIRMED,
CATEGORY_ASSIGNED and CORRECTED. Creation captures the starting receipt fields;
subsequent entries contain only changed fields with old and new values. Dates use
ISO strings and amounts use decimal strings. Image paths, signed links, credentials
and provider payloads are excluded. Each event is inserted in the same transaction
as its receipt change; if either fails, both roll back. Existing owner locks
serialize changes so concurrent corrections retain a coherent before/after chain.
Re-saving an unchanged value creates no correction entry. Failed validation,
duplicates, and failed transactions create no history entry.

Existing receipts are not backfilled: unknown past changes are not reconstructed.
They begin accumulating audit entries with their next actual application change.
This is application-maintained history, not a tamper-proof database ledger:
manual SQL edits outside the application are not captured. The existing RETRY
operation still removes an unconfirmed draft; its history is removed by the
composite receipt/owner foreign key's ON DELETE CASCADE. Completed receipts are
not deleted by RETRY.

### Phase 8 deployment order

1. Run the full disposable PostgreSQL test suite before deployment.
2. Back up the live database. In Supabase SQL Editor, run
   `migrations/005_receipt_audit.sql` once, after migration 004, under the existing
   table-owning migration role. Do not rerun migrations 001–004.
3. Confirm success, then push/deploy the new backend on Render. Do not deploy the
   audit-writing backend before migration 005 exists.
4. Use `/history <receipt-id>` to inspect a receipt. Existing receipts initially
   have no history. Make an intentional correction with `/edit` and verify its
   old/new values in `/history`. Repeating the same value should add no entry.

Migration 005 only adds a table, constraints and index; it changes no existing
receipt values and performs no backfill. Foreign-key creation can briefly lock
receipts; the migration has a 5-second lock timeout and a 60-second statement
timeout and rolls back on error. RLS is enabled with no client policies. Backend
access uses the existing privileged direct database role; anon/authenticated API
clients are not given history access. If the application is rolled back, leave
the additive history table in place to preserve its data.


## Phase 9: explicit vendor aliases

Use `/vendors` to see vendors whose categories you have learned through the
receipt conversation. Use an existing name from that list as the target:

```text
/alias Starbucks #1234 | Starbucks
/aliases
/unalias Starbucks #1234
```

Both lists accept a page number (`/vendors 2`, `/aliases 2`) and show ten entries
per page. Names accept spaces; the single `|` separates alternate and target
names. Aliases match exactly after the existing alphanumeric uppercase
normalization. Case and punctuation variations already match without an alias.
There is no fuzzy matching, automatic branch-number stripping or global memory.

An explicit alias uses the target's current learned category for future receipts
in this Telegram chat. It does not copy category memory, so later category-learning
changes at the target are reflected in future alias lookups. Direct learned vendor
memory takes precedence. Targets must be learned vendors, not aliases. Names with
existing memory, conflicting alias targets, self-links, and links affecting an
open receipt for the alternate name are rejected. Repeating the same link is
idempotent. Remove a link before assigning it to a different target. Writes share
the existing per-user lock with receipt creation/category learning.

Receipts retain their extracted vendor names and normalized keys. Historical
categories, audit entries, receipt searches and duplicate constraints are unchanged;
this phase links category recognition, not financial records. Different alias names
are not merged into one duplicate key. Removing an alias affects only future
category lookups. Alias commands do not resolve a pending conversation, and the
conversational model cannot add or remove aliases. Receipt audit history does not
record alias management because no receipt changes during that operation.

### Phase 9 deployment order

Run the full disposable PostgreSQL suite first. Back up the database, then run
`migrations/006_vendor_aliases.sql` once in Supabase under the existing migration
role, after migration 005. Do not rerun migrations 001–005. This is an additive
backend-only table with RLS enabled and no client policies. A composite foreign
key keeps targets in the same user's category memory. No receipt or memory rows
are backfilled or rewritten. Foreign-key creation can briefly lock the memory
table; 5-second lock and 60-second statement timeouts bound the migration.

Only after migration 006 succeeds, push/deploy this backend. Smoke-test `/vendors`,
add an alternate spelling linked to one of your learned vendors, inspect `/aliases`,
and remove the test link with `/unalias`. Automated pipeline tests verify that
matching new receipts inherit the target category while keeping their original
vendor names and image-duplicate protection. Leave the additive table in place if
rolling back the application.


## Phase 10: CSV export in Telegram

Send `/export 2026-09` to download September 2026, or `/export all` for all dates.
The bot sends a CSV document to the requesting chat, containing only that chat's
completed receipts. Columns are date, vendor, category, total, VAT, and status.
Rows are ordered by receipt date then ID; month boundaries include the first day
and exclude the next month's first day. No pending/failed/review receipts appear.
Exports reflect current corrections and original vendor names, without merging
aliases. No model is called and pending conversations are not resolved.

CSV uses UTF-8 with BOM for Excel, ISO dates, two decimal places, quoted fields,
and blank VAT when not shown (zero VAT remains 0.00). Category/vendor text with a
spreadsheet formula prefix is prefixed with an apostrophe to keep it as text;
CSV quoting preserves commas, quotes and embedded newlines. That protective
apostrophe may be visible in some importers. No credentials, image links, storage
paths, chat IDs, or user IDs are exported. Existing single-currency assumptions
apply; there is no currency conversion.

Files are generated in memory and uploaded directly through Telegram sendDocument,
not written to disk or Supabase. The attachment remains available in the Telegram
chat under normal Telegram behavior; it is not an expiring receipt-image link.
An empty selection sends a message instead of an attachment. Each export is bounded
to 10,000 receipts and 5 MiB. Larger selections are rejected with guidance to
export one month; they are never silently truncated. Upload errors are recorded
as failures without storing provider payloads or claiming successful delivery.
Send the command again to retry a failed export.

No migration, package installation, or new environment variable is required.
Run the full disposable PostgreSQL suite, push, and wait for Render deployment.
Smoke-test `/export 2026-09`: open the attachment in Excel or Google Sheets and
compare its row count and total with `/summary 2026-09`. Also test `/export all`.
Automated tests use fake Telegram HTTP responses and never send real documents.


## Phase 11: Private web dashboard

The existing FastAPI service serves `/dashboard/`; no separate frontend build or
host is required. In a private Telegram chat, send `/dashboard` and open the
one-time sign-in link within 10 minutes. Browser sessions last 24 hours; Sign out
revokes that session. A new link invalidates earlier unused links, but does not
revoke other signed-in browsers. Treat sign-in links as passwords.

Views include monthly spending and category totals, paginated receipts with vendor, category,
and status filters, audited corrections, receipt images, change history, review
confirmation/category assignment, learned vendors, alternate-name management, and
monthly CSV downloads. Existing repository validation and ownership checks apply.
Review drafts remain excluded from totals. Dashboard changes are visible through
Telegram commands, but do not send unsolicited Telegram notifications.

Login/session tokens are stored only as SHA-256 hashes. Login tokens travel in the
URL fragment, which is removed before exchanging them. Sessions use Secure,
HttpOnly, SameSite=Strict cookies. POST requests require the configured Origin.
Responses disable caching; receipt images use existing 15-minute signed links.
No Supabase credentials or storage paths are exposed to browser code. Only positive
(private) Telegram chat IDs may request dashboard sign-in.

### Phase 11 deployment order

1. Run the full disposable PostgreSQL test suite.
2. Apply `migrations/007_web_dashboard.sql` once after 006 in Supabase. It adds
   only the backend token table with RLS and no client policies; existing receipts
   and category memory remain unchanged. Do not rerun earlier migrations.
3. Set Render `DASHBOARD_URL` to the exact HTTPS origin of this backend, such as
   `https://your-service.onrender.com` (no path, query, or fragment). Leaving it
   empty disables sign-in and the dashboard API.
4. Push/deploy the backend, then send `/dashboard` in your private bot chat.
5. Compare September 2026 totals with `/summary 2026-09`, open a receipt, make and
   restore a test category correction, inspect its history, export CSV, and sign out.

Existing environment variables and hosting remain unchanged. Rolling back the
application may leave migration 007 in place. Automated tests use synthetic data
and fake storage responses, with PostgreSQL migration and concurrent login tests.


### Sign-in page and public demo

Set `TELEGRAM_BOT_USERNAME` in Render to the bot's public username without `@`
(for example `ExampleReceiptBot`) to show the Open Telegram bot button. This is
public configuration, not the bot token. Without it, the manual sign-in steps
remain available. The page also offers a copyable `/dashboard` command, explains
the 10-minute link and 24-hour browser session, and treats a signed-out visit as
a normal state. Expired or used sign-in links still display their error.

`/dashboard/demo` is a public read-only preview containing hard-coded fictional
receipts. It does not query the database, load receipt photos, create a session,
or relax authentication for any private API. It may be shared with GitHub visitors.
No migration or new dependency is needed for these sign-in improvements.
