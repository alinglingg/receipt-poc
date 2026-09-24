# Receipt POC

Telegram receipt-processing proof of concept for the Full Stack Automation and AI Developer assessment.

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
