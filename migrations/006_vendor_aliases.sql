-- Apply once after 005, before deploying the vendor-alias backend.
-- Existing receipts, memory, audit entries and duplicate constraints are unchanged.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE vendor_aliases (
    user_id uuid NOT NULL,
    normalized_name text NOT NULL,
    display_name text NOT NULL,
    target_normalized text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, normalized_name),
    CONSTRAINT vendor_aliases_target_fkey FOREIGN KEY (user_id, target_normalized)
        REFERENCES user_vendor_memory(user_id, normalized_name),
    CONSTRAINT vendor_aliases_not_self CHECK (normalized_name <> target_normalized)
);
ALTER TABLE vendor_aliases ENABLE ROW LEVEL SECURITY;
COMMIT;
