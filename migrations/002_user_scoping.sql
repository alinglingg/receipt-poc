-- Apply once, after 001_init.sql, with Telegram processing stopped.
-- Existing tables, rows, chat IDs, duplicate constraints and image paths are retained.
-- Run with a migration role that owns the tables. Any error rolls back the transaction.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

LOCK TABLE webhook_events, receipts, pending_conversations, vendor_memory
  IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM receipts r JOIN webhook_events e ON e.id = r.event_id
    WHERE r.chat_id <> e.chat_id
  ) THEN
    RAISE EXCEPTION 'Ownership backfill aborted: receipt and event chats differ';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pending_conversations p JOIN receipts r ON r.id = p.receipt_id
    WHERE p.chat_id <> r.chat_id
  ) THEN
    RAISE EXCEPTION 'Ownership backfill aborted: pending conversation and receipt chats differ';
  END IF;
END $$;

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_chat_id bigint UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT users_id_chat_key UNIQUE (id, telegram_chat_id)
);

INSERT INTO users (telegram_chat_id)
SELECT chat_id FROM webhook_events
UNION SELECT chat_id FROM receipts
UNION SELECT chat_id FROM pending_conversations;

ALTER TABLE receipts ADD COLUMN user_id uuid REFERENCES users(id);
UPDATE receipts r SET user_id = u.id FROM users u WHERE u.telegram_chat_id = r.chat_id;
ALTER TABLE receipts ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE receipts
  ADD CONSTRAINT receipts_user_chat_fkey FOREIGN KEY (user_id, chat_id)
    REFERENCES users(id, telegram_chat_id),
  ADD CONSTRAINT receipts_user_exact_duplicate_key
    UNIQUE (user_id, vendor_normalized, receipt_date, total_amount),
  ADD CONSTRAINT receipts_id_user_key UNIQUE (id, user_id);

ALTER TABLE pending_conversations ADD COLUMN user_id uuid REFERENCES users(id);
UPDATE pending_conversations p SET user_id = r.user_id FROM receipts r WHERE r.id = p.receipt_id;
ALTER TABLE pending_conversations ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE pending_conversations
  ADD CONSTRAINT pending_conversations_user_chat_fkey FOREIGN KEY (user_id, chat_id)
    REFERENCES users(id, telegram_chat_id),
  ADD CONSTRAINT pending_conversations_receipt_user_fkey FOREIGN KEY (receipt_id, user_id)
    REFERENCES receipts(id, user_id);
CREATE UNIQUE INDEX pending_conversations_one_open_per_user
  ON pending_conversations (user_id) WHERE status = 'OPEN';

CREATE TABLE user_vendor_memory (
  user_id uuid NOT NULL REFERENCES users(id),
  normalized_name text NOT NULL,
  display_name text NOT NULL,
  category text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, normalized_name)
);

-- Learn only from each user's own completed receipts with an unambiguous category.
-- The legacy global memory is retained, never broadcast to unrelated users.
INSERT INTO user_vendor_memory (user_id, normalized_name, display_name, category)
SELECT user_id, vendor_normalized, min(vendor_name), min(category)
FROM receipts
WHERE status = 'COMPLETED' AND category IS NOT NULL AND btrim(category) <> ''
GROUP BY user_id, vendor_normalized
HAVING count(DISTINCT category) = 1;

-- These new tables are backend-only; no browser/anonymous access policies.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_vendor_memory ENABLE ROW LEVEL SECURITY;
COMMIT;
