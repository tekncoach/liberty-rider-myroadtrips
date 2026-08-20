-- Where each named col sits along its ride, so a marker can be redrawn from
-- this table alone (see the ride_cols comment in db.py's SCHEMA).
--
-- This is what lets the public share page show the same cols as the owner's
-- modal without recomputing them: reading a col is a SELECT, only *finding*
-- one costs an Overpass call. Existing rows keep their name and get NULL
-- positions until that ride's detail is next opened, at which point
-- api_ride_cols replaces them wholesale.

ALTER TABLE ride_cols ADD COLUMN IF NOT EXISTS distance_km DOUBLE PRECISION;
ALTER TABLE ride_cols ADD COLUMN IF NOT EXISTS elevation DOUBLE PRECISION;
ALTER TABLE ride_cols ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE ride_cols ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION;
