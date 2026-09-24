-- Apply once after 003, with receipt processing paused and before Phase 4 deploy.
-- Preserves existing receipts, pending categories, memory and duplicate constraints.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
LOCK TABLE webhook_events, receipts IN SHARE ROW EXCLUSIVE MODE;
ALTER TABLE receipts ADD COLUMN raw_date_text text;
ALTER TABLE webhook_events DROP CONSTRAINT IF EXISTS webhook_events_status_check;
ALTER TABLE webhook_events DROP CONSTRAINT IF EXISTS webhook_events_valid_status;
ALTER TABLE webhook_events ADD CONSTRAINT webhook_events_valid_status CHECK (
  status IN ('RECEIVED', 'PROCESSING', 'PENDING_CATEGORY', 'NEEDS_REVIEW', 'COMPLETED',
             'DUPLICATE', 'RETRY_REQUESTED', 'FAILED')
);
-- Historical raw date text is unknown: keep NULL, do not reclassify old receipts.
COMMIT;
