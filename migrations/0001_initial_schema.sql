-- Initial Postgres schema (hosted deployments only — SQLite has its own
-- ALTER-TABLE migration path in db.py, unrelated to this directory).
--
-- RLS is enabled on every table with no policies — deny-all by default.
-- The app connects as the table owner (bypasses RLS), so this only
-- matters as defense in depth if these tables were ever reachable through
-- Supabase's Data API (they aren't meant to be).

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  first_name TEXT,
  email TEXT,
  bearer_token TEXT,
  refresh_token TEXT,
  firebase_api_key TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roadtrips (
  id SERIAL PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rides (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT,
  start_time TEXT NOT NULL,
  distance DOUBLE PRECISION,
  duration DOUBLE PRECISION,
  duration_without_pauses DOUBLE PRECISION,
  total_pauses_duration DOUBLE PRECISION,
  pause_count INTEGER,
  maximum_altitude DOUBLE PRECISION,
  is_favorite INTEGER,
  hidden INTEGER,
  state TEXT,
  detailed_polyline TEXT,
  preview_picture_url TEXT,
  gpx_export_url TEXT,
  gpx_local_path TEXT,
  start_lat DOUBLE PRECISION, start_lon DOUBLE PRECISION,
  stop_lat DOUBLE PRECISION, stop_lon DOUBLE PRECISION,
  vehicle_brand TEXT, vehicle_model TEXT, vehicle_type TEXT, vehicle_cc DOUBLE PRECISION,
  created_roadbook_id TEXT,
  roadtrip_id INTEGER REFERENCES roadtrips(id),
  merged_into TEXT REFERENCES rides(id),
  notes TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS pauses (
  ride_id TEXT REFERENCES rides(id),
  estimated_time TEXT,
  automatic INTEGER,
  lat DOUBLE PRECISION, lon DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS resumes (
  ride_id TEXT REFERENCES rides(id),
  estimated_time TEXT,
  automatic INTEGER,
  lat DOUBLE PRECISION, lon DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS elevation_cache (
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  elevation DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (lat, lon)
);

CREATE TABLE IF NOT EXISTS mountain_pass_cache (
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  name TEXT,
  PRIMARY KEY (lat, lon)
);

CREATE TABLE IF NOT EXISTS ride_cols (
  ride_id TEXT REFERENCES rides(id),
  name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ride_cols_ride ON ride_cols(ride_id);

CREATE TABLE IF NOT EXISTS sync_state (
  user_id TEXT REFERENCES users(id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS tags (
  id SERIAL PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT NOT NULL,
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS ride_tags (
  ride_id TEXT REFERENCES rides(id),
  tag_id INTEGER REFERENCES tags(id),
  PRIMARY KEY (ride_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_rides_start_time ON rides(start_time);
CREATE INDEX IF NOT EXISTS idx_rides_roadtrip ON rides(roadtrip_id);
CREATE INDEX IF NOT EXISTS idx_rides_merged_into ON rides(merged_into);
CREATE INDEX IF NOT EXISTS idx_rides_user ON rides(user_id);
CREATE INDEX IF NOT EXISTS idx_pauses_ride ON pauses(ride_id);
CREATE INDEX IF NOT EXISTS idx_resumes_ride ON resumes(ride_id);
CREATE INDEX IF NOT EXISTS idx_ride_tags_tag ON ride_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE roadtrips ENABLE ROW LEVEL SECURITY;
ALTER TABLE rides ENABLE ROW LEVEL SECURITY;
ALTER TABLE pauses ENABLE ROW LEVEL SECURITY;
ALTER TABLE resumes ENABLE ROW LEVEL SECURITY;
ALTER TABLE elevation_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE mountain_pass_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE ride_cols ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE ride_tags ENABLE ROW LEVEL SECURITY;
