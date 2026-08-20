-- Server-side session deadlines (finding APP-03): the 30-day cookie Max-Age
-- was only ever a browser-side courtesy, so a stolen session id stayed valid
-- forever.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

-- Backfilling existing rows is deliberately NOT done here: a migration runs
-- once, and db._backfill_session_expiry() runs on every boot on both
-- backends, which is the stronger guarantee (and keeps the two paths
-- testable by the same test).
