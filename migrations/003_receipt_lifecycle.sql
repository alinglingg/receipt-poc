-- Apply once after 002_user_scoping.sql, before deploying the Phase 3 backend.
-- Stop Telegram processing while applying. All changes roll back on error.
-- Existing receipt values, owners, categories, image paths and duplicate rules remain.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
LOCK TABLE webhook_events, receipts, pending_conversations, processing_attempts
  IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE webhook_events
  ADD COLUMN processing_started_at timestamptz,
  ADD COLUMN completed_at timestamptz;
ALTER TABLE receipts
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN processing_started_at timestamptz,
  ADD COLUMN completed_at timestamptz,
  ADD COLUMN review_reason text,
  ADD COLUMN failure_reason text;

-- Replace the status check created by 001, or the equivalent ORM-created check.
-- No tables or columns are dropped.
ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_status_check;
ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_valid_status;
ALTER TABLE receipts ADD CONSTRAINT receipts_valid_status CHECK (
  status IN ('PROCESSING', 'PENDING_CATEGORY', 'NEEDS_REVIEW', 'COMPLETED', 'DUPLICATE', 'FAILED')
);

-- Historical start times use the earliest recorded attempt, when available.
-- Unknown times stay NULL rather than pretending the migration was processing.
UPDATE webhook_events e SET processing_started_at = a.started_at
FROM (SELECT event_id, min(created_at) AS started_at FROM processing_attempts GROUP BY event_id) a
WHERE a.event_id = e.id;
UPDATE webhook_events SET completed_at = updated_at
WHERE status IN ('COMPLETED', 'DUPLICATE', 'RETRY_REQUESTED', 'FAILED');

UPDATE receipts r SET
  processing_started_at = e.processing_started_at,
  updated_at = greatest(r.created_at, e.updated_at),
  completed_at = CASE WHEN r.status = 'COMPLETED' THEN
    coalesce((SELECT p.resolved_at FROM pending_conversations p WHERE p.receipt_id = r.id),
             CASE WHEN e.status = 'COMPLETED' THEN e.updated_at END)
    ELSE NULL END
FROM webhook_events e WHERE e.id = r.event_id;

COMMIT;
