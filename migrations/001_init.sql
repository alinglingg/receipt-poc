CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE webhook_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  update_id bigint UNIQUE NOT NULL,
  chat_id bigint NOT NULL,
  kind text NOT NULL CHECK (kind IN ('photo', 'text', 'other')),
  file_id text,
  text text,
  status text NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED', 'PROCESSING', 'PENDING_CATEGORY', 'COMPLETED', 'DUPLICATE', 'RETRY_REQUESTED', 'FAILED')),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE receipts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid UNIQUE NOT NULL REFERENCES webhook_events(id) ON DELETE CASCADE,
  chat_id bigint NOT NULL,
  vendor_name text NOT NULL,
  vendor_normalized text NOT NULL,
  receipt_date date NOT NULL,
  total_amount numeric(12,2) NOT NULL,
  vat_amount numeric(12,2),
  category text,
  confidence text NOT NULL CHECK (confidence IN ('High', 'Medium', 'Low')),
  status text NOT NULL CHECK (status IN ('PENDING_CATEGORY', 'COMPLETED')),
  image_path text NOT NULL,
  image_sha256 text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chat_id, vendor_normalized, receipt_date, total_amount)
);

CREATE TABLE vendor_memory (
  normalized_name text PRIMARY KEY,
  display_name text NOT NULL,
  category text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pending_conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id bigint NOT NULL,
  receipt_id uuid UNIQUE NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

CREATE UNIQUE INDEX pending_conversations_one_open_per_chat
  ON pending_conversations (chat_id) WHERE status = 'OPEN';

CREATE TABLE processing_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES webhook_events(id) ON DELETE CASCADE,
  stage text NOT NULL,
  attempt_number int NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
  success boolean NOT NULL,
  error_code text,
  latency_ms int CHECK (latency_ms IS NULL OR latency_ms >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX processing_attempts_event_id_idx ON processing_attempts (event_id, created_at DESC);
