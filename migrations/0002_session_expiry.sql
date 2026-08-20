-- Server-side session deadlines (finding APP-03): the 30-day cookie Max-Age
-- was only ever a browser-side courtesy, so a stolen session id stayed valid
-- forever. Nullable on purpose — rows predating this column are treated as
-- still valid by get_session_user() and are cleaned up as they age out.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
