-- Apply once after 006, before enabling DASHBOARD_URL in Render.
-- Contains only token hashes. No existing receipts, users or memory are rewritten.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE browser_tokens (
    token_hash varchar(64) PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind varchar(16) NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT browser_tokens_valid_kind CHECK (kind IN ('LOGIN', 'SESSION'))
);
CREATE INDEX browser_tokens_user_expiry ON browser_tokens(user_id, expires_at);
ALTER TABLE browser_tokens ENABLE ROW LEVEL SECURITY;
COMMIT;
