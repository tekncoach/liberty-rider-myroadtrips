"""FastAPI backend for the "Mes trajets" personal roadtrip organizer.

Multi-tenant: every request that touches ride/roadtrip/tag data requires a
valid session (see `get_session_user`), and every query is scoped to that
session's user. The only way to establish a session is POST /api/auth/login
with an email + password, exchanged directly with Firebase — see
firebase_refresh.py. There is no other login path.
"""
from __future__ import annotations

import os
import pathlib
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

app = FastAPI(title="Mes trajets — Liberty Rider")
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


def _merge_group(conn, row) -> list:
    """All raw rows forming this ride's merge group, chronological — just
    `[row]` if it isn't merged with anything."""
    members = _merge_members(conn, row["id"])
    if not members:
        return [row]
    return sorted([row, *members], key=lambda r: r["start_time"])


def _merged_ride_dict(conn, row) -> dict:
    """Effective, user-facing view of a ride: if it's a merge representative,
    its stats/name/location are the aggregate of its whole group; the raw
    `rides` rows underneath are never mutated, so un-merging is lossless."""
    group = _merge_group(conn, row)
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
    return {"ok": True, "first_name": lr_user.get("firstName")}


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
    return {"logged_in": True, "first_name": user["first_name"]}


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
        rides = [_merged_ride_dict(conn, r) for r in rows]
        _attach_tags_bulk(conn, rides)
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


@app.get("/api/rides/{ride_id}/elevation")
def api_ride_elevation(ride_id: str, user=Depends(get_session_user)):
    """Elevation profile along the track, resampled to ~60 points by
    distance, plus each pause projected onto its nearest track point (fun
    detail: spot a pause taken at the highest point of the ride, e.g. for a
    photo). Estimated via open-elevation (see elevation.py) since Liberty
    Rider itself has no per-point altitude data at all."""
    conn = db.connect()
    try:
        row = _get_owned_ride(conn, ride_id, user["id"])
        points, pauses = _merged_polyline_and_pauses(conn, row)
        if not points:
            return {"profile": [], "pauses": []}

        cum = [0.0]
        for i in range(1, len(points)):
            cum.append(cum[-1] + (haversine_km(*points[i - 1], *points[i]) or 0))

        sample_idx = _resample_indices(cum, target_count=60)
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
            }
            for k, i in enumerate(pause_idx)
        ]
        return {"profile": profile, "pauses": pause_markers}
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

def _roadtrip_summary(conn, trip_row, ride_rows) -> dict:
    rides = [_merged_ride_dict(conn, r) for r in ride_rows]
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
        out = []
        for t in trips:
            rides = conn.execute(
                "SELECT * FROM rides WHERE roadtrip_id = ? ORDER BY start_time", (t["id"],)
            ).fetchall()
            out.append(_roadtrip_summary(conn, t, rides))
        return out
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
        rides = [_merged_ride_dict(conn, r) for r in ride_rows]
        polylines, pauses = _polylines_and_pauses(conn, ride_rows)

        summary = _roadtrip_summary(conn, trip, ride_rows)
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
            "INSERT INTO roadtrips (user_id, name) VALUES (?, ?)", (user["id"], req.name)
        )
        trip_id = cur.lastrowid
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

def _tag_summary(conn, tag_row, ride_rows) -> dict:
    rides = [_merged_ride_dict(conn, r) for r in ride_rows]
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
        return [_tag_summary(conn, t, _tag_ride_rows(conn, t["id"])) for t in tags]
    finally:
        conn.close()


@app.get("/api/tags/{tag_id}")
def api_tag_detail(tag_id: int, user=Depends(get_session_user)):
    conn = db.connect()
    try:
        tag = _get_owned_tag(conn, tag_id, user["id"])
        ride_rows = _tag_ride_rows(conn, tag_id)
        rides = [_merged_ride_dict(conn, r) for r in ride_rows]
        polylines, pauses = _polylines_and_pauses(conn, ride_rows)

        summary = _tag_summary(conn, tag, ride_rows)
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
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
