-- Suggestions are unconfirmed and never replace saved categories.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE receipts ADD COLUMN suggested_category text;
COMMIT;
