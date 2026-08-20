-- Server-side session deadlines (finding APP-03): the 30-day cookie Max-Age
-- was only ever a browser-side courtesy, so a stolen session id stayed valid
-- forever.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

-- Existing rows are backfilled from created_at instead of being left NULL.
-- A NULL row would be accepted indefinitely by get_session_user() and
-- skipped by delete_expired_sessions() — immortal and unpurgeable.
UPDATE sessions
   SET expires_at = to_char(created_at::timestamp + interval '30 days', 'YYYY-MM-DD HH24:MI:SS')
 WHERE expires_at IS NULL;
