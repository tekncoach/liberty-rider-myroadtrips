"""FastAPI backend for "Carnet de Route", a personal roadtrip organizer
that syncs ride history from a Liberty Rider account.

Multi-tenant: every request that touches ride/roadtrip/tag data requires a
valid session (see `get_session_user`), and every query is scoped to that
session's user. The only way to establish a session is POST /api/auth/login
with an email + password, exchanged directly with Firebase — see
firebase_refresh.py. There is no other login path.
"""
from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager
from datetime import timedelta

import gpxpy
import gpxpy.gpx
import polyline as polyline_lib
import requests
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.responses import Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import elevation
import mountain_pass
import sync as sync_module
from firebase_refresh import refresh_id_token, sign_in_with_password
from liberty_client import LibertyRiderClient
from utils import day_key, haversine_km, parse_iso

ROOT = pathlib.Path(__file__).parent
STATIC = ROOT / "static"

SESSION_COOKIE = "session_id"
# Liberty Rider's Firebase web API key — public by design (Firebase web API
# keys identify the project, they don't grant access on their own), captured
# from the site's own login network traffic. Override via env var if it
# ever rotates.
DEFAULT_FIREBASE_API_KEY = "AIzaSyCMLiqF3hrzqnf4ltuQjLMPSPh19rgDL5c"
FIREBASE_API_KEY = os.environ.get("LIBERTY_RIDER_FIREBASE_API_KEY", DEFAULT_FIREBASE_API_KEY)
# Set COOKIE_SECURE=1 behind HTTPS (any real deployment) so the session
# cookie is never sent over plain HTTP. Left off by default for local dev.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
# Comma-separated Liberty Rider user ids (currentUser.id) allowed to use
# maintainer-only tools (currently: purging one's own synced data to
# re-test onboarding) — not a feature end users are meant to see or use.
ADMIN_USER_IDS = {u.strip() for u in os.environ.get("ADMIN_USER_IDS", "").split(",") if u.strip()}

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Not `yield`ed to for TestClient(app) used without a `with` block
    # (every test fixture here) — db.init_db() below still runs eagerly
    # at import time so tests are unaffected; this only handles a clean
    # pool shutdown for the real server process.
    yield
    if db.IS_POSTGRES:
        db._POOL.close()


app = FastAPI(title="Carnet de Route (Liberty Rider sync)", lifespan=_lifespan)
db.init_db()


# --- auth plumbing -----------------------------------------------------

def get_session_user(session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if not session_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    conn = db.connect()
    try:
        user = db.get_session_user(conn, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Session expired — log in again")
        return user
    finally:
        conn.close()


def _live_client_for(conn, user) -> LibertyRiderClient:
    """A LibertyRiderClient using this user's saved bearer token, transparently
    refreshed via their saved Firebase refresh token if it's expired."""
    token = user["bearer_token"]
    if not token:
        raise HTTPException(status_code=401, detail="No Liberty Rider token on file — log in again")
    client = LibertyRiderClient(token)
    try:
        client.get_current_user()
        return client
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status not in (401, 403) or not user["refresh_token"] or not user["firebase_api_key"]:
            raise
    refreshed = refresh_id_token(user["refresh_token"], user["firebase_api_key"])
    db.save_user_tokens(conn, user["id"], refreshed["id_token"], refreshed["refresh_token"], None)
    return LibertyRiderClient(refreshed["id_token"])


def ride_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "start_time": row["start_time"],
        "distance": row["distance"],
        "duration": row["duration"],
        "duration_without_pauses": row["duration_without_pauses"],
        "total_pauses_duration": row["total_pauses_duration"],
        "pause_count": row["pause_count"],
        "maximum_altitude": row["maximum_altitude"],
        "is_favorite": bool(row["is_favorite"]),
        "hidden": bool(row["hidden"]),
        "state": row["state"],
        "start_lat": row["start_lat"], "start_lon": row["start_lon"],
        "stop_lat": row["stop_lat"], "stop_lon": row["stop_lon"],
        "vehicle_brand": row["vehicle_brand"], "vehicle_model": row["vehicle_model"],
        "created_roadbook_id": row["created_roadbook_id"],
        "roadtrip_id": row["roadtrip_id"],
        "preview_picture_url": row["preview_picture_url"],
        "merged_into": row["merged_into"],
        "notes": row["notes"],
    }


def _raw_pauses(conn, ride_id: str) -> list[dict]:
    pause_rows = conn.execute(
        "SELECT * FROM pauses WHERE ride_id = ? ORDER BY estimated_time", (ride_id,)
    ).fetchall()
    return [
        {
            "estimated_time": p["estimated_time"],
            "automatic": bool(p["automatic"]),
            "lat": p["lat"],
            "lon": p["lon"],
        }
        for p in pause_rows
    ]


def _raw_resumes(conn, ride_id: str) -> list[dict]:
    resume_rows = conn.execute(
        "SELECT * FROM resumes WHERE ride_id = ? ORDER BY estimated_time", (ride_id,)
    ).fetchall()
    return [
        {
            "estimated_time": r["estimated_time"],
            "automatic": bool(r["automatic"]),
            "lat": r["lat"],
            "lon": r["lon"],
        }
        for r in resume_rows
    ]


def _merge_members(conn, ride_id: str):
    """Raw rows absorbed into `ride_id` via a merge (empty if none)."""
    return conn.execute(
        "SELECT * FROM rides WHERE merged_into = ? ORDER BY start_time", (ride_id,)
    ).fetchall()


def _merge_members_map(conn, user_id: str) -> dict:
    """Every absorbed merge-member this user has, keyed by its
    representative's id — one query for a whole request (list endpoints
    iterate many rides) instead of one `_merge_members` query per ride."""
    rows = conn.execute(
        "SELECT * FROM rides WHERE user_id = ? AND merged_into IS NOT NULL ORDER BY start_time",
        (user_id,),
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["merged_into"], []).append(r)
    return out


def _merge_group(conn, row, members_map=None) -> list:
    """All raw rows forming this ride's merge group, chronological — just
    `[row]` if it isn't merged with anything. Pass `members_map` (from
    `_merge_members_map`) when processing many rides in one request to
    avoid a separate query per ride."""
    members = members_map.get(row["id"], []) if members_map is not None else _merge_members(conn, row["id"])
    if not members:
        return [row]
    return sorted([row, *members], key=lambda r: r["start_time"])


def _merged_ride_dict(conn, row, members_map=None) -> dict:
    """Effective, user-facing view of a ride: if it's a merge representative,
    its stats/name/location are the aggregate of its whole group; the raw
    `rides` rows underneath are never mutated, so un-merging is lossless."""
    group = _merge_group(conn, row, members_map)
    base = ride_row_to_dict(row)
    base["merge_ride_ids"] = [r["id"] for r in group]
    if len(group) == 1:
        return base

    from datetime import timedelta
    from utils import parse_iso

    first, last = group[0], group[-1]
    end_dt = parse_iso(last["start_time"]) + timedelta(seconds=last["duration"] or 0)
    merged_duration = (end_dt - parse_iso(first["start_time"])).total_seconds()
    riding_time = sum(r["duration_without_pauses"] or 0 for r in group)
    altitudes = [r["maximum_altitude"] for r in group if r["maximum_altitude"] is not None]
    base.update({
        "name": f"{first['name'] or '?'} → {last['name'] or '?'}",
        "start_time": first["start_time"],
        "distance": sum(r["distance"] or 0 for r in group),
        "duration": merged_duration,
        "duration_without_pauses": riding_time,
        "total_pauses_duration": max(merged_duration - riding_time, 0),
        "pause_count": sum(r["pause_count"] or 0 for r in group) + (len(group) - 1),
        "maximum_altitude": max(altitudes) if altitudes else None,
        "start_lat": first["start_lat"], "start_lon": first["start_lon"],
        "stop_lat": last["stop_lat"], "stop_lon": last["stop_lon"],
    })
    return base


def _merged_polyline_and_pauses(conn, row) -> tuple[list, list]:
    group = _merge_group(conn, row)
    points = []
    for r in group:
        if r["detailed_polyline"]:
            points.extend(polyline_lib.decode(r["detailed_polyline"]))
    pauses = []
    for r in group:
        pauses.extend(_raw_pauses(conn, r["id"]))
    return points, pauses


def _ride_own_timeline(start_time: str, duration: float, pauses: list[dict], resumes: list[dict]) -> list[dict]:
    """Ride/pause segments for a single raw ride's own pause+resume events.
    State always starts "ride" at the ride's own start_time — this matches
    how Liberty Rider computes that ride's own duration_without_pauses /
    total_pauses_duration (verified numerically against real data), so a
    trailing unresolved pause (no matching resume before this ride ends)
    gets capped at this ride's own end rather than left open."""
    start = parse_iso(start_time)
    end = start + timedelta(seconds=duration or 0)
    events = sorted(
        [(parse_iso(p["estimated_time"]), "pause") for p in pauses]
        + [(parse_iso(r["estimated_time"]), "resume") for r in resumes],
        key=lambda e: e[0],
    )
    segments = []
    state = "ride"
    seg_start = start
    for t, etype in events:
        if t <= seg_start:
            continue
        if etype == "pause" and state == "ride":
            segments.append({"type": "ride", "start": seg_start, "end": t})
            state, seg_start = "pause", t
        elif etype == "resume" and state == "pause":
            segments.append({"type": "pause", "start": seg_start, "end": t})
            state, seg_start = "ride", t
        # pause-while-paused / resume-while-riding: no counterpart in our
        # state, ignore rather than fabricate or misattribute a segment.
    if end > seg_start:
        segments.append({"type": state, "start": seg_start, "end": end})
    return segments


def _merged_ride_timeline(conn, row) -> list[dict]:
    """Concatenates each merge-group member's own independently-built
    timeline (see _ride_own_timeline) across merge boundaries. Any
    inter-member tracking gap is counted as "pause" — consistent with how
    the merged ride's own total_pauses_duration is computed (merged
    duration minus each member's own riding time, so the untracked gap
    already counts as pause time there too).

    Building each member's timeline independently (rather than merge-sorting
    every pause/resume across the whole group at once) matters: on real
    data, a member ride's dangling unresolved pause otherwise gets "closed"
    by an unrelated resume belonging to the *next* member's own later pause,
    ballooning one segment to cover everything in between."""
    def _append(segments, seg):
        if segments and segments[-1]["type"] == seg["type"] and segments[-1]["end"] == seg["start"]:
            segments[-1]["end"] = seg["end"]
        else:
            segments.append(seg)

    group = _merge_group(conn, row)
    segments: list[dict] = []
    prev_end = None
    for r in group:
        member_start = parse_iso(r["start_time"])
        if prev_end is not None and member_start > prev_end:
            _append(segments, {"type": "pause", "start": prev_end, "end": member_start})
        member_segments = _ride_own_timeline(
            r["start_time"], r["duration"] or 0, _raw_pauses(conn, r["id"]), _raw_resumes(conn, r["id"])
        )
        for seg in member_segments:
            _append(segments, seg)
        prev_end = member_start + timedelta(seconds=r["duration"] or 0)
    return [
        {"type": s["type"], "start": s["start"].isoformat(), "end": s["end"].isoformat()}
        for s in segments
    ]


def _polylines_and_pauses(conn, ride_rows) -> tuple[dict, dict]:
    polylines = {}
    pauses = {}
    for r in ride_rows:
        points, p = _merged_polyline_and_pauses(conn, r)
        polylines[r["id"]] = points
        pauses[r["id"]] = p
    return polylines, pauses


def _resample_indices(cum: list[float], target_count: int) -> list[int]:
    """Picks ~target_count point indices evenly spaced by cumulative
    distance (not raw index) — GPS points cluster tighter at low speed or
    during a pause, so index-based sampling would over-represent them."""
    n = len(cum)
    if n <= target_count:
        return list(range(n))
    total = cum[-1]
    if total == 0:
        step = (n - 1) / (target_count - 1)
        return sorted({round(i * step) for i in range(target_count)})
    step = total / (target_count - 1)
    indices = []
    idx = 0
    for k in range(target_count):
        target = step * k
        while idx < n - 1 and cum[idx] < target:
            idx += 1
        indices.append(idx)
    if indices[-1] != n - 1:
        indices.append(n - 1)
    return sorted(set(indices))


def _nearest_point_index(points: list[tuple[float, float]], lat: float, lon: float) -> int:
    best_i, best_d = 0, float("inf")
    for i, (plat, plon) in enumerate(points):
        d = (plat - lat) ** 2 + (plon - lon) ** 2
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def _detect_peaks(profile: list[dict], min_prominence_m: float = 100) -> list[int]:
    """Indices (into `profile`) of local elevation maxima with at least
    `min_prominence_m` of prominence over the rest of the ride's own
    profile. A col is defined by shape — it climbs, then descends — not by
    absolute altitude, so a 300m pass and a 2000m pass are detected the
    same way."""
    indices = []
    n = len(profile)
    for i in range(1, n - 1):
        cur, prev, nxt = profile[i]["elevation"], profile[i - 1]["elevation"], profile[i + 1]["elevation"]
        if cur is None or prev is None or nxt is None or not (cur > prev and cur > nxt):
            continue
        left_min = min((p["elevation"] for p in profile[:i + 1] if p["elevation"] is not None), default=cur)
        right_min = min((p["elevation"] for p in profile[i:] if p["elevation"] is not None), default=cur)
        if cur - max(left_min, right_min) >= min_prominence_m:
            indices.append(i)
    return indices


def _refine_peak_point(conn, points, sample_idx, pauses, peak_i, window_samples=15):
    """The 60-point resampled profile is coarse (~5km apart on a long
    ride) — a detected peak's coarse sample can land geographically closer
    to a nearby col than to the true summit a few hundred meters away
    (seen on real data: Mont Ventoux vs. the adjacent Col des Tempêtes).
    Refine by densely sampling the raw GPS points between the peak's
    neighboring coarse samples, plus any pause recorded along that same
    stretch (a rider stopping at a summit is often a very precise fix on
    exactly where the true high point was) — whichever candidate has the
    highest estimated elevation wins."""
    lo = sample_idx[peak_i - 1] if peak_i > 0 else sample_idx[peak_i]
    hi = sample_idx[peak_i + 1] if peak_i < len(sample_idx) - 1 else sample_idx[peak_i]
    if hi <= lo:
        lo = hi = sample_idx[peak_i]
    step = max((hi - lo) // window_samples, 1)
    window_idx = sorted(set(range(lo, hi + 1, step)) | {hi})
    candidates = [points[i] for i in window_idx]

    for p in pauses:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        if lo <= _nearest_point_index(points, p["lat"], p["lon"]) <= hi:
            candidates.append((p["lat"], p["lon"]))

    elevations = elevation.get_elevations(conn, candidates)
    known = [(pt, e) for pt, e in zip(candidates, elevations) if e is not None]
    if not known:
        return points[sample_idx[peak_i]], None
    (lat, lon), best_elev = max(known, key=lambda t: t[1])
    return (lat, lon), best_elev


def _group_by_day(rides: list[dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for r in rides:
        by_day.setdefault(day_key(r["start_time"]), []).append(r)
    days = []
    for d in sorted(by_day.keys()):
        day_rides = by_day[d]
        days.append({
            "date": d,
            "ride_ids": [r["id"] for r in day_rides],
            "total_distance": sum(r["distance"] or 0 for r in day_rides),
            "total_duration": sum(r["duration"] or 0 for r in day_rides),
            "total_duration_without_pauses": sum(
                r["duration_without_pauses"] or 0 for r in day_rides
            ),
            "total_pause_count": sum(r["pause_count"] or 0 for r in day_rides),
        })
    return days


def _build_gpx(conn, ride_rows) -> str:
    gpx = gpxpy.gpx.GPX()
    for r in ride_rows:
        points, _ = _merged_polyline_and_pauses(conn, r)
        if not points:
            continue
        track = gpxpy.gpx.GPXTrack(name=r["name"] or r["start_time"])
        gpx.tracks.append(track)
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        for lat, lon in points:
            segment.points.append(gpxpy.gpx.GPXTrackPoint(lat, lon))
    return gpx.to_xml()


def _gpx_response(xml: str, name: str) -> RawResponse:
    filename = f"{name}.gpx".replace("/", "-")
    return RawResponse(
        content=xml,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _ride_tags(conn, ride_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.name FROM tags t JOIN ride_tags rt ON rt.tag_id = t.id "
        "WHERE rt.ride_id = ? ORDER BY t.name",
        (ride_id,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def _attach_tags_bulk(conn, rides: list[dict]) -> None:
    """Sets `tags` on each ride dict in place with one query instead of N+1."""
    by_ride: dict[str, list[dict]] = {r["id"]: [] for r in rides}
    if not by_ride:
        return
    placeholders = ",".join("?" * len(by_ride))
    rows = conn.execute(
        f"SELECT rt.ride_id, t.id, t.name FROM ride_tags rt JOIN tags t ON t.id = rt.tag_id "
        f"WHERE rt.ride_id IN ({placeholders}) ORDER BY t.name",
        list(by_ride.keys()),
    ).fetchall()
    for row in rows:
        by_ride[row["ride_id"]].append({"id": row["id"], "name": row["name"]})
    for r in rides:
        r["tags"] = by_ride[r["id"]]


def _attach_col_names_bulk(conn, rides: list[dict]) -> None:
    """Sets `col_names` on each ride dict in place — searchable col names
    discovered so far (see api_ride_cols), empty for a ride never opened."""
    by_ride: dict[str, list[str]] = {r["id"]: [] for r in rides}
    if not by_ride:
        return
    placeholders = ",".join("?" * len(by_ride))
    rows = conn.execute(
        f"SELECT ride_id, name FROM ride_cols WHERE ride_id IN ({placeholders})",
        list(by_ride.keys()),
    ).fetchall()
    for row in rows:
        by_ride[row["ride_id"]].append(row["name"])
    for r in rides:
        r["col_names"] = by_ride[r["id"]]


def add_suggestions(rides: list[dict]) -> None:
    """Flag ungrouped rides adjacent to their chronological neighbor among other
    ungrouped rides — a hint for manual grouping, never an automatic assignment.
    Also flags a tighter "merge candidate" pair — same day, near-zero gap and
    near-identical location — the classic Liberty Rider tracking-cut-in-two
    case, with the concrete neighbor id so the UI can offer a one-click merge.
    `rides` may include already-grouped/tagged rides (e.g. when listing
    everything for the "Mes traces" filter) — those are skipped."""
    for r in rides:
        r["suggested_link_prev"] = False
        r["suggested_link_next"] = False
        r["merge_candidate_prev_id"] = None
        r["merge_candidate_next_id"] = None
    candidates = [
        r for r in rides
        if r["roadtrip_id"] is None and not r.get("tags") and not r.get("merge_ride_ids", [r["id"]])[1:]
    ]
    ordered = sorted(candidates, key=lambda r: r["start_time"])
    for i, ride in enumerate(ordered):
        if i > 0:
            prev = ordered[i - 1]
            ride["suggested_link_prev"] = _is_adjacent(prev, ride)
            if _is_merge_candidate(prev, ride):
                ride["merge_candidate_prev_id"] = prev["id"]
        if i < len(ordered) - 1:
            nxt = ordered[i + 1]
            ride["suggested_link_next"] = _is_adjacent(ride, nxt)
            if _is_merge_candidate(ride, nxt):
                ride["merge_candidate_next_id"] = nxt["id"]


def _gap_hours_and_distance(earlier: dict, later: dict) -> tuple[float, float | None] | None:
    from utils import parse_iso
    try:
        earlier_end = parse_iso(earlier["start_time"])
        if earlier.get("duration"):
            from datetime import timedelta
            earlier_end += timedelta(seconds=earlier["duration"])
        gap_hours = (parse_iso(later["start_time"]) - earlier_end).total_seconds() / 3600
    except Exception:
        return None
    if gap_hours < 0:
        return None
    dist_km = haversine_km(
        earlier["stop_lat"], earlier["stop_lon"], later["start_lat"], later["start_lon"]
    )
    return gap_hours, dist_km


def _is_adjacent(earlier: dict, later: dict) -> bool:
    result = _gap_hours_and_distance(earlier, later)
    if result is None:
        return False
    gap_hours, dist_km = result
    same_day = day_key(earlier["start_time"]) == day_key(later["start_time"])
    close = dist_km is not None and dist_km < 5
    return same_day or (gap_hours < 24 and close)


def _is_merge_candidate(earlier: dict, later: dict) -> bool:
    """Tighter than _is_adjacent: near-zero gap and near-identical location —
    almost certainly the same real ride, split by a tracking hiccup."""
    result = _gap_hours_and_distance(earlier, later)
    if result is None:
        return False
    gap_hours, dist_km = result
    return gap_hours < (30 / 60) and dist_km is not None and dist_km < 1.5


class LoginRequest(BaseModel):
    email: str
    password: str


class SyncRequest(BaseModel):
    full: bool = False


class CreateRoadtripRequest(BaseModel):
    name: str
    ride_ids: list[str] = []


class AttachTagRequest(BaseModel):
    name: str


class SetNotesRequest(BaseModel):
    notes: str


class RenameTagRequest(BaseModel):
    name: str


class RenameRoadtripRequest(BaseModel):
    name: str


class AddRidesRequest(BaseModel):
    ride_ids: list[str]


class MergeRidesRequest(BaseModel):
    ride_ids: list[str]


# --- auth ---------------------------------------------------------------

@app.post("/api/auth/login")
def api_login(req: LoginRequest, response: Response):
    if not FIREBASE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: LIBERTY_RIDER_FIREBASE_API_KEY isn't set.",
        )
    try:
        tokens = sign_in_with_password(req.email, req.password, FIREBASE_API_KEY)
    except requests.HTTPError as e:
        message = "Login failed"
        if e.response is not None:
            try:
                message = e.response.json().get("error", {}).get("message", message)
            except ValueError:
                pass
        raise HTTPException(status_code=401, detail=message)

    client = LibertyRiderClient(tokens["id_token"])
    try:
        lr_user = client.get_current_user()
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Liberty Rider API error: {e}")

    conn = db.connect()
    try:
        user_id = lr_user["id"]
        db.upsert_user(conn, user_id, lr_user.get("firstName"), req.email)
        db.save_user_tokens(conn, user_id, tokens["id_token"], tokens["refresh_token"], FIREBASE_API_KEY)
        db.claim_orphaned_data(conn, user_id)
        session_id = db.create_session(conn, user_id)
    finally:
        conn.close()

    response.set_cookie(
        SESSION_COOKIE, session_id, httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True, "first_name": lr_user.get("firstName"), "is_admin": user_id in ADMIN_USER_IDS}


@app.post("/api/auth/logout")
def api_logout(response: Response, session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if session_id:
        conn = db.connect()
        try:
            db.delete_session(conn, session_id)
        finally:
            conn.close()
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/status")
def api_auth_status(session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if not session_id:
        return {"logged_in": False}
    conn = db.connect()
    try:
        user = db.get_session_user(conn, session_id)
    finally:
        conn.close()
    if not user:
        return {"logged_in": False}
    return {"logged_in": True, "first_name": user["first_name"], "is_admin": user["id"] in ADMIN_USER_IDS}


@app.get("/api/auth/profile")
def api_auth_profile(user=Depends(get_session_user)):
    conn = db.connect()
    try:
        client = _live_client_for(conn, user)
        lr_user = client.get_current_user()
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Liberty Rider API error: {e}")
    finally:
        conn.close()
    return {"first_name": lr_user.get("firstName") or user["first_name"], "manual_ride_count": lr_user.get("manualRideCount")}


@app.delete("/api/account/data")
def api_purge_account_data(user=Depends(get_session_user)):
    """Maintainer-only: wipes every ride/roadtrip/tag synced locally for this
    account (not the Liberty Rider account itself), to re-test onboarding
    from a genuinely empty state. Gated on ADMIN_USER_IDS — not something
    regular users should see or be able to trigger on their own data."""
    if user["id"] not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="Not available on this account")
    conn = db.connect()
    try:
        db.purge_user_data(conn, user["id"])
    finally:
        conn.close()
    return {"ok": True}


# --- sync -----------------------------------------------------------------

@app.post("/api/sync")
def api_sync(req: SyncRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        client = _live_client_for(conn, user)
    finally:
        conn.close()
    try:
        summary = sync_module.sync(client, user["id"], full=req.full)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            raise HTTPException(status_code=401, detail="Token expired or invalid — log in again.")
        raise HTTPException(status_code=502, detail=f"Liberty Rider API error: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return summary


# --- rides ------------------------------------------------------------

@app.get("/api/rides")
def api_list_rides(grouped: bool | None = None, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        # merged_into IS NULL: a ride absorbed into another via a merge is
        # never listed on its own — only its merge representative is.
        if grouped is None:
            rows = conn.execute(
                "SELECT * FROM rides WHERE user_id = ? AND merged_into IS NULL ORDER BY start_time DESC",
                (user["id"],),
            ).fetchall()
        elif grouped:
            rows = conn.execute(
                "SELECT * FROM rides WHERE user_id = ? AND roadtrip_id IS NOT NULL AND merged_into IS NULL "
                "ORDER BY start_time DESC",
                (user["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rides WHERE user_id = ? AND roadtrip_id IS NULL AND merged_into IS NULL "
                "ORDER BY start_time DESC",
                (user["id"],),
            ).fetchall()
        members_map = _merge_members_map(conn, user["id"])
        rides = [_merged_ride_dict(conn, r, members_map) for r in rows]
        _attach_tags_bulk(conn, rides)
        _attach_col_names_bulk(conn, rides)
        add_suggestions(rides)
        return rides
    finally:
        conn.close()


def _get_owned_ride(conn, ride_id: str, user_id: str):
    row = conn.execute("SELECT * FROM rides WHERE id = ? AND user_id = ?", (ride_id, user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Ride not found")
    return row


@app.get("/api/rides/{ride_id}")
def api_ride_detail(ride_id: str, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        row = _get_owned_ride(conn, ride_id, user["id"])
        ride = _merged_ride_dict(conn, row)
        ride["polyline"], ride["pauses"] = _merged_polyline_and_pauses(conn, row)
        ride["timeline"] = _merged_ride_timeline(conn, row)
        ride["tags"] = _ride_tags(conn, ride_id)
        return ride
    finally:
        conn.close()


def _ride_track_samples(conn, row):
    """Decoded polyline, cumulative distance per point, and ~60 indices
    sampled evenly by distance — shared by /elevation and /cols so both
    resample the same track the same way."""
    points, pauses = _merged_polyline_and_pauses(conn, row)
    if not points:
        return points, pauses, [], []
    cum = [0.0]
    for i in range(1, len(points)):
        cum.append(cum[-1] + (haversine_km(*points[i - 1], *points[i]) or 0))
    sample_idx = _resample_indices(cum, target_count=60)
    return points, pauses, cum, sample_idx


@app.get("/api/rides/{ride_id}/elevation")
def api_ride_elevation(ride_id: str, user=Depends(get_session_user)):
    """Elevation profile along the track, resampled to ~60 points by
    distance, plus each pause projected onto its nearest track point (fun
    detail: spot a pause taken at the highest point of the ride, e.g. for a
    photo). Estimated via open-elevation (see elevation.py) since Liberty
    Rider itself has no per-point altitude data at all.

    Named mountain passes are a separate endpoint (/cols) — not computed
    here — since Overpass lookups are one network call per candidate peak
    and can be slow; this endpoint must stay fast so the chart itself
    always renders instantly, even on a ride with no cols at all."""
    conn = db.connect()
    try:
        row = _get_owned_ride(conn, ride_id, user["id"])
        points, pauses, cum, sample_idx = _ride_track_samples(conn, row)
        if not points:
            return {"profile": [], "pauses": [], "elevation_gain": None, "elevation_loss": None}

        valid_pauses = [p for p in pauses if p["lat"] is not None and p["lon"] is not None]
        pause_idx = [_nearest_point_index(points, p["lat"], p["lon"]) for p in valid_pauses]

        all_points = [points[i] for i in sample_idx] + [points[i] for i in pause_idx]
        elevations = elevation.get_elevations(conn, all_points)

        profile = [
            {"distance_km": round(cum[i], 2), "elevation": elevations[k]}
            for k, i in enumerate(sample_idx)
        ]
        pause_markers = [
            {
                "distance_km": round(cum[i], 2),
                "elevation": elevations[len(sample_idx) + k],
                "automatic": valid_pauses[k]["automatic"],
                "lat": valid_pauses[k]["lat"],
                "lon": valid_pauses[k]["lon"],
            }
            for k, i in enumerate(pause_idx)
        ]
        # D+/D- summed over the ~60-point resampled profile, not raw GPS —
        # already an estimate (open-elevation, not measured), so this is an
        # approximation of an approximation; good enough for a fun stat, not
        # a precise instrument reading.
        known_elevations = [p["elevation"] for p in profile if p["elevation"] is not None]
        gain = sum(max(b - a, 0) for a, b in zip(known_elevations, known_elevations[1:]))
        loss = sum(max(a - b, 0) for a, b in zip(known_elevations, known_elevations[1:]))

        return {
            "profile": profile,
            "pauses": pause_markers,
            "elevation_gain": round(gain) if known_elevations else None,
            "elevation_loss": round(loss) if known_elevations else None,
        }
    finally:
        conn.close()


@app.get("/api/rides/{ride_id}/cols")
def api_ride_cols(ride_id: str, user=Depends(get_session_user)):
    """Named mountain passes along the track (see mountain_pass.py and
    _detect_peaks) — kept separate from /elevation precisely because this
    is the slow part (a network round-trip per candidate peak); the
    frontend fetches it after the chart has already rendered."""
    conn = db.connect()
    try:
        row = _get_owned_ride(conn, ride_id, user["id"])
        points, pauses, cum, sample_idx = _ride_track_samples(conn, row)
        if not points:
            return {"cols": []}

        elevations = elevation.get_elevations(conn, [points[i] for i in sample_idx])
        profile = [
            {"distance_km": round(cum[i], 2), "elevation": elevations[k]}
            for k, i in enumerate(sample_idx)
        ]

        # Only named passes are worth marking — an unnamed peak is just
        # noise on the chart, so unnamed detections are silently dropped.
        valid_pauses = [p for p in pauses if p.get("lat") is not None and p.get("lon") is not None]
        cols = []
        for i in _detect_peaks(profile):
            (lat, lon), refined_elev = _refine_peak_point(conn, points, sample_idx, valid_pauses, i)
            name = mountain_pass.get_pass_name(conn, lat, lon, hint_elevation=refined_elev)
            if name:
                cols.append({
                    "distance_km": profile[i]["distance_km"],
                    "elevation": refined_elev if refined_elev is not None else profile[i]["elevation"],
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                })

        # Persisted so these names become searchable (see api_list_rides) —
        # progressively, only for rides whose detail has actually been
        # opened, not backfilled for the whole history at once.
        conn.execute("DELETE FROM ride_cols WHERE ride_id = ?", (ride_id,))
        conn.executemany(
            "INSERT INTO ride_cols (ride_id, name) VALUES (?, ?)",
            [(ride_id, c["name"]) for c in cols],
        )
        conn.commit()

        return {"cols": cols}
    finally:
        conn.close()


@app.post("/api/rides/merge")
def api_merge_rides(req: MergeRidesRequest, user=Depends(get_session_user)):
    if len(req.ride_ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 ride ids to merge")
    conn = db.connect()
    try:
        placeholders = ",".join("?" * len(req.ride_ids))
        rows = conn.execute(
            f"SELECT * FROM rides WHERE id IN ({placeholders}) AND user_id = ?",
            [*req.ride_ids, user["id"]],
        ).fetchall()
        if len(rows) != len(req.ride_ids):
            raise HTTPException(status_code=404, detail="One or more rides not found")
        roadtrip_ids = set()
        for r in rows:
            if r["merged_into"] is not None:
                raise HTTPException(status_code=400, detail=f"Ride {r['id']} is already part of a merge")
            if _merge_members(conn, r["id"]):
                raise HTTPException(status_code=400, detail=f"Ride {r['id']} already has rides merged into it")
            if _ride_tags(conn, r["id"]):
                raise HTTPException(status_code=400, detail=f"Ride {r['id']} is tagged — untag it before merging")
            if r["roadtrip_id"] is not None:
                roadtrip_ids.add(r["roadtrip_id"])
        if len(roadtrip_ids) > 1:
            raise HTTPException(
                status_code=400,
                detail="These rides belong to different roadtrips — detach one before merging",
            )
        common_roadtrip_id = next(iter(roadtrip_ids), None)

        ordered = sorted(rows, key=lambda r: r["start_time"])
        representative = ordered[0]
        for r in ordered[1:]:
            conn.execute("UPDATE rides SET merged_into = ? WHERE id = ?", (representative["id"], r["id"]))
        if common_roadtrip_id is not None:
            conn.execute(
                "UPDATE rides SET roadtrip_id = ? WHERE id = ?",
                (common_roadtrip_id, representative["id"]),
            )
        conn.commit()
        return {"id": representative["id"]}
    finally:
        conn.close()


@app.delete("/api/rides/{ride_id}/merge")
def api_unmerge_ride(ride_id: str, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_ride(conn, ride_id, user["id"])
        conn.execute("UPDATE rides SET merged_into = NULL WHERE merged_into = ?", (ride_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/rides/{ride_id}/tags")
def api_attach_tag(ride_id: str, req: AttachTagRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_ride(conn, ride_id, user["id"])
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tag name must not be empty")
        conn.execute(
            "INSERT INTO tags (user_id, name) VALUES (?, ?) ON CONFLICT(user_id, name) DO NOTHING",
            (user["id"], name),
        )
        tag = conn.execute(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?", (user["id"], name)
        ).fetchone()
        conn.execute(
            "INSERT INTO ride_tags (ride_id, tag_id) VALUES (?, ?) ON CONFLICT(ride_id, tag_id) DO NOTHING",
            (ride_id, tag["id"]),
        )
        conn.commit()
        return {"tags": _ride_tags(conn, ride_id)}
    finally:
        conn.close()


@app.delete("/api/rides/{ride_id}/tags/{tag_id}")
def api_detach_tag(ride_id: str, tag_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_ride(conn, ride_id, user["id"])
        conn.execute(
            "DELETE FROM ride_tags WHERE ride_id = ? AND tag_id = ? "
            "AND tag_id IN (SELECT id FROM tags WHERE user_id = ?)",
            (ride_id, tag_id, user["id"]),
        )
        conn.commit()
        return {"tags": _ride_tags(conn, ride_id)}
    finally:
        conn.close()


@app.patch("/api/rides/{ride_id}/notes")
def api_set_ride_notes(ride_id: str, req: SetNotesRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_ride(conn, ride_id, user["id"])
        notes = req.notes.strip()
        conn.execute("UPDATE rides SET notes = ? WHERE id = ?", (notes or None, ride_id))
        conn.commit()
        return {"notes": notes}
    finally:
        conn.close()


@app.get("/api/rides/{ride_id}/export.gpx")
def api_export_ride_gpx(ride_id: str, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        row = _get_owned_ride(conn, ride_id, user["id"])
        return _gpx_response(_build_gpx(conn, [row]), row["name"] or row["id"])
    finally:
        conn.close()


@app.delete("/api/rides/{ride_id}/roadtrip")
def api_detach_ride(ride_id: str, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_ride(conn, ride_id, user["id"])
        conn.execute("UPDATE rides SET roadtrip_id = NULL WHERE id = ?", (ride_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- roadtrips --------------------------------------------------------

def _roadtrip_summary(conn, trip_row, ride_rows, members_map=None) -> dict:
    rides = [_merged_ride_dict(conn, r, members_map) for r in ride_rows]
    total_distance = sum(r["distance"] or 0 for r in rides)
    total_duration = sum(r["duration"] or 0 for r in rides)
    total_pauses = sum(r["pause_count"] or 0 for r in rides)
    days = sorted({day_key(r["start_time"]) for r in rides})
    if days:
        from utils import parse_iso
        span_days = (parse_iso(days[-1]).date() - parse_iso(days[0]).date()).days + 1
    else:
        span_days = 0
    return {
        "id": trip_row["id"],
        "name": trip_row["name"],
        "ride_count": len(rides),
        "day_count": span_days,
        "start_date": days[0] if days else None,
        "end_date": days[-1] if days else None,
        "total_distance": total_distance,
        "total_duration": total_duration,
        "total_pause_count": total_pauses,
    }


def _get_owned_roadtrip(conn, trip_id: int, user_id: str):
    row = conn.execute("SELECT * FROM roadtrips WHERE id = ? AND user_id = ?", (trip_id, user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Roadtrip not found")
    return row


@app.get("/api/roadtrips")
def api_list_roadtrips(user=Depends(get_session_user)):
    conn = db.connect()
    try:
        trips = conn.execute(
            "SELECT * FROM roadtrips WHERE user_id = ? ORDER BY id DESC", (user["id"],)
        ).fetchall()
        members_map = _merge_members_map(conn, user["id"])
        # One query for every roadtrip's rides instead of one query per
        # roadtrip — grouped by roadtrip_id in Python below.
        all_rides = conn.execute(
            "SELECT * FROM rides WHERE user_id = ? AND roadtrip_id IS NOT NULL ORDER BY start_time",
            (user["id"],),
        ).fetchall()
        rides_by_trip: dict = {}
        for r in all_rides:
            rides_by_trip.setdefault(r["roadtrip_id"], []).append(r)
        return [_roadtrip_summary(conn, t, rides_by_trip.get(t["id"], []), members_map) for t in trips]
    finally:
        conn.close()


@app.get("/api/roadtrips/{trip_id}")
def api_roadtrip_detail(trip_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        trip = _get_owned_roadtrip(conn, trip_id, user["id"])
        ride_rows = conn.execute(
            "SELECT * FROM rides WHERE roadtrip_id = ? ORDER BY start_time", (trip_id,)
        ).fetchall()
        members_map = _merge_members_map(conn, user["id"])
        rides = [_merged_ride_dict(conn, r, members_map) for r in ride_rows]
        polylines, pauses = _polylines_and_pauses(conn, ride_rows)

        summary = _roadtrip_summary(conn, trip, ride_rows, members_map)
        summary["rides"] = rides
        summary["polylines"] = polylines
        summary["pauses"] = pauses
        summary["days"] = _group_by_day(rides)
        return summary
    finally:
        conn.close()


@app.post("/api/roadtrips")
def api_create_roadtrip(req: CreateRoadtripRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO roadtrips (user_id, name) VALUES (?, ?) RETURNING id",
            (user["id"], req.name),
        )
        trip_id = cur.fetchone()["id"]
        if req.ride_ids:
            placeholders = ",".join("?" * len(req.ride_ids))
            conn.execute(
                f"UPDATE rides SET roadtrip_id = ? WHERE id IN ({placeholders}) AND user_id = ?",
                [trip_id, *req.ride_ids, user["id"]],
            )
        conn.commit()
        return {"id": trip_id}
    finally:
        conn.close()


@app.patch("/api/roadtrips/{trip_id}")
def api_rename_roadtrip(trip_id: int, req: RenameRoadtripRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_roadtrip(conn, trip_id, user["id"])
        conn.execute("UPDATE roadtrips SET name = ? WHERE id = ?", (req.name, trip_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/roadtrips/{trip_id}")
def api_delete_roadtrip(trip_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_roadtrip(conn, trip_id, user["id"])
        conn.execute("UPDATE rides SET roadtrip_id = NULL WHERE roadtrip_id = ?", (trip_id,))
        conn.execute("DELETE FROM roadtrips WHERE id = ?", (trip_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/roadtrips/{trip_id}/rides")
def api_add_rides(trip_id: int, req: AddRidesRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_roadtrip(conn, trip_id, user["id"])
        placeholders = ",".join("?" * len(req.ride_ids))
        conn.execute(
            f"UPDATE rides SET roadtrip_id = ? WHERE id IN ({placeholders}) AND user_id = ?",
            [trip_id, *req.ride_ids, user["id"]],
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/roadtrips/{trip_id}/export.gpx")
def api_export_gpx(trip_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        trip = _get_owned_roadtrip(conn, trip_id, user["id"])
        ride_rows = conn.execute(
            "SELECT * FROM rides WHERE roadtrip_id = ? ORDER BY start_time", (trip_id,)
        ).fetchall()
        return _gpx_response(_build_gpx(conn, ride_rows), trip["name"] or "roadtrip")
    finally:
        conn.close()


# --- tags ---------------------------------------------------------------

def _tag_summary(conn, tag_row, ride_rows, members_map=None) -> dict:
    rides = [_merged_ride_dict(conn, r, members_map) for r in ride_rows]
    total_distance = sum(r["distance"] or 0 for r in rides)
    total_duration = sum(r["duration"] or 0 for r in rides)
    total_pauses = sum(r["pause_count"] or 0 for r in rides)
    days = sorted({day_key(r["start_time"]) for r in rides})
    return {
        "id": tag_row["id"],
        "name": tag_row["name"],
        "ride_count": len(rides),
        "day_count": len(days),
        "start_date": days[0] if days else None,
        "end_date": days[-1] if days else None,
        "total_distance": total_distance,
        "total_duration": total_duration,
        "total_pause_count": total_pauses,
    }


def _tag_ride_rows(conn, tag_id: int):
    return conn.execute(
        "SELECT r.* FROM rides r JOIN ride_tags rt ON rt.ride_id = r.id "
        "WHERE rt.tag_id = ? ORDER BY r.start_time",
        (tag_id,),
    ).fetchall()


def _get_owned_tag(conn, tag_id: int, user_id: str):
    row = conn.execute("SELECT * FROM tags WHERE id = ? AND user_id = ?", (tag_id, user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tag not found")
    return row


@app.get("/api/tags")
def api_list_tags(user=Depends(get_session_user)):
    conn = db.connect()
    try:
        tags = conn.execute(
            "SELECT * FROM tags WHERE user_id = ? ORDER BY name", (user["id"],)
        ).fetchall()
        members_map = _merge_members_map(conn, user["id"])
        # One query for every tag's rides instead of one query per tag.
        all_rows = conn.execute(
            "SELECT r.*, rt.tag_id AS tag_id_group FROM rides r "
            "JOIN ride_tags rt ON rt.ride_id = r.id JOIN tags t ON t.id = rt.tag_id "
            "WHERE t.user_id = ? ORDER BY r.start_time",
            (user["id"],),
        ).fetchall()
        rides_by_tag: dict = {}
        for r in all_rows:
            rides_by_tag.setdefault(r["tag_id_group"], []).append(r)
        return [_tag_summary(conn, t, rides_by_tag.get(t["id"], []), members_map) for t in tags]
    finally:
        conn.close()


@app.get("/api/tags/{tag_id}")
def api_tag_detail(tag_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        tag = _get_owned_tag(conn, tag_id, user["id"])
        ride_rows = _tag_ride_rows(conn, tag_id)
        members_map = _merge_members_map(conn, user["id"])
        rides = [_merged_ride_dict(conn, r, members_map) for r in ride_rows]
        polylines, pauses = _polylines_and_pauses(conn, ride_rows)

        summary = _tag_summary(conn, tag, ride_rows, members_map)
        summary["rides"] = rides
        summary["polylines"] = polylines
        summary["pauses"] = pauses
        summary["days"] = _group_by_day(rides)
        return summary
    finally:
        conn.close()


@app.patch("/api/tags/{tag_id}")
def api_rename_tag(tag_id: int, req: RenameTagRequest, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_tag(conn, tag_id, user["id"])
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tag name must not be empty")
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/tags/{tag_id}")
def api_delete_tag(tag_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        _get_owned_tag(conn, tag_id, user["id"])
        conn.execute("DELETE FROM ride_tags WHERE tag_id = ?", (tag_id,))
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/tags/{tag_id}/export.gpx")
def api_export_tag_gpx(tag_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        tag = _get_owned_tag(conn, tag_id, user["id"])
        ride_rows = _tag_ride_rows(conn, tag_id)
        return _gpx_response(_build_gpx(conn, ride_rows), tag["name"] or "tag")
    finally:
        conn.close()


@app.get("/")
def landing():
    return FileResponse(STATIC / "landing.html")


@app.get("/app")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
