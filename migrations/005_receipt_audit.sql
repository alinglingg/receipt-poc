-- Apply once after 004, before deploying the audit-writing backend.
-- Additive only: existing receipts and their values are not changed or backfilled.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE receipt_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id uuid NOT NULL,
    user_id uuid NOT NULL,
    event_type varchar(32) NOT NULL,
    old_value json,
    new_value json NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT receipt_events_receipt_user_fkey
        FOREIGN KEY (receipt_id, user_id) REFERENCES receipts(id, user_id) ON DELETE CASCADE,
    CONSTRAINT receipt_events_valid_type CHECK
        (event_type IN ('CREATED', 'CATEGORY_ASSIGNED', 'REVIEW_CONFIRMED', 'CORRECTED'))
);
CREATE INDEX receipt_events_owner_receipt_time
    ON receipt_events(user_id, receipt_id, created_at, id);
-- Backend uses the existing privileged DB connection; no client API policies.
ALTER TABLE receipt_events ENABLE ROW LEVEL SECURITY;
COMMIT;
