-- Public share links (see docs/PLAN-public-share.md).
--
-- One row per token EVER ISSUED, never deleted: revoking sets `revoked_at`,
-- regenerating revokes the active row and inserts a new one. Keeping revoked
-- rows is what guarantees a regenerated token can never resurrect an old
-- link — the PRIMARY KEY spans active and revoked tokens alike — and leaves
-- an auditable trail of what was once public.
--
-- `expires_at` is never set by the current app; the lookup query already
-- honours it, so adding time-limited links later needs no schema change.
--
-- RLS enabled with no policies (deny-all), like every other table here.

CREATE TABLE IF NOT EXISTS ride_shares (
  token TEXT PRIMARY KEY,
  ride_id TEXT NOT NULL REFERENCES rides(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  expires_at TEXT
);

-- At most one ACTIVE share per ride (revoked ones accumulate freely), so the
-- UI never has to reason about a list of live links for one ride.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ride_shares_active
  ON ride_shares(ride_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ride_shares_user ON ride_shares(user_id);

ALTER TABLE ride_shares ENABLE ROW LEVEL SECURITY;
