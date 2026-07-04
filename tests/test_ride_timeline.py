"""_ride_own_timeline / _merged_ride_timeline: the trajet/pause chronology
shown on a ride's detail modal.

Both are exercised at two levels:
- direct unit calls for the state-machine edge cases (spurious/unresolved
  events), independent of any DB or merge setup;
- through GET /api/rides/{id} after a real merge, for the actual bug this
  was built to fix — a member ride's dangling unresolved pause must not be
  "closed" by an unrelated resume belonging to a different member's pause.
"""
import db as db_module
from conftest import make_event, make_ride


# --- _ride_own_timeline (unit) ----------------------------------------------


def test_ride_own_timeline_alternates_ride_and_pause(app_module):
    pauses = [{"estimated_time": "2024-01-01T10:30:00Z"}]
    resumes = [{"estimated_time": "2024-01-01T10:40:00Z"}]
    segments = app_module._ride_own_timeline("2024-01-01T10:00:00Z", 3600, pauses, resumes)

    assert [s["type"] for s in segments] == ["ride", "pause", "ride"]
    assert segments[0]["start"].isoformat() == "2024-01-01T10:00:00+00:00"
    assert segments[0]["end"].isoformat() == "2024-01-01T10:30:00+00:00"
    assert segments[1]["end"].isoformat() == "2024-01-01T10:40:00+00:00"
    assert segments[2]["end"].isoformat() == "2024-01-01T11:00:00+00:00"  # ride start + 3600s


def test_ride_own_timeline_caps_unresolved_trailing_pause_at_ride_end(app_module):
    # No resume at all — the pause must run to the ride's own end, not stay open.
    pauses = [{"estimated_time": "2024-01-01T10:30:00Z"}]
    segments = app_module._ride_own_timeline("2024-01-01T10:00:00Z", 3600, pauses, [])

    assert [s["type"] for s in segments] == ["ride", "pause"]
    assert segments[1]["end"].isoformat() == "2024-01-01T11:00:00+00:00"


def test_ride_own_timeline_ignores_resume_while_riding(app_module):
    # A resume with no preceding pause (seen on real data — Liberty Rider can
    # log a resume for a brief pause-at-start it never logs as a pause) must
    # not fabricate a pause segment.
    resumes = [{"estimated_time": "2024-01-01T10:10:00Z"}]
    segments = app_module._ride_own_timeline("2024-01-01T10:00:00Z", 1800, [], resumes)

    assert [s["type"] for s in segments] == ["ride"]
    assert segments[0]["end"].isoformat() == "2024-01-01T10:30:00+00:00"


def test_ride_own_timeline_ignores_pause_while_already_paused(app_module):
    # Two pause events with no resume in between — the second is a no-op,
    # not a fresh pause segment.
    pauses = [
        {"estimated_time": "2024-01-01T10:10:00Z"},
        {"estimated_time": "2024-01-01T10:20:00Z"},
    ]
    resumes = [{"estimated_time": "2024-01-01T10:40:00Z"}]
    segments = app_module._ride_own_timeline("2024-01-01T10:00:00Z", 3600, pauses, resumes)

    assert [s["type"] for s in segments] == ["ride", "pause", "ride"]
    assert segments[1]["start"].isoformat() == "2024-01-01T10:10:00+00:00"
    assert segments[1]["end"].isoformat() == "2024-01-01T10:40:00+00:00"


# --- _merged_ride_timeline (via API, real merge) ----------------------------


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def test_merged_timeline_does_not_let_next_members_resume_close_a_dangling_pause(client, login_as):
    """The actual bug: member 1 has a pause with no resume before its own
    end; merge-sorting every event across the whole group used to let
    member 2's own (unrelated) resume close it, ballooning one pause segment
    to swallow real riding time from member 2."""
    login_as(client, "user-1")

    # Member 1: starts 10:00, runs 1h (ends 11:00), pauses at 10:30 with no
    # resume of its own — a dangling pause, common when tracking cuts out.
    _seed_ride(
        "user-1", "r1", "2024-01-01T10:00:00Z",
        duration=3600.0, duration_without_pauses=1800.0, total_pauses_duration=1800.0, pause_count=1,
        pauses=[make_event("2024-01-01T10:30:00Z")],
    )
    # Member 2: starts 11:20 (a 20min tracking gap after member 1 ends),
    # pauses at 11:50 and resumes at 12:00 — its own, unrelated cycle.
    _seed_ride(
        "user-1", "r2", "2024-01-01T11:20:00Z",
        duration=3600.0, duration_without_pauses=3000.0, total_pauses_duration=600.0, pause_count=1,
        pauses=[make_event("2024-01-01T11:50:00Z")],
        resumes=[make_event("2024-01-01T12:00:00Z")],
    )

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200
    rep_id = resp.json()["id"]

    timeline = client.get(f"/api/rides/{rep_id}").json()["timeline"]

    # There must be a real "ride" segment covering member 2's actual riding
    # window (11:20->11:50) — the bug made this vanish into one giant pause.
    ride_segments = [s for s in timeline if s["type"] == "ride"]
    assert any(s["start"].startswith("2024-01-01T11:20:00") for s in ride_segments), timeline

    # Member 1's dangling pause must be capped at its own end (11:00), not
    # bridged all the way to member 2's resume (12:00).
    first_pause = next(s for s in timeline if s["type"] == "pause")
    assert first_pause["start"].startswith("2024-01-01T10:30:00")
    assert not first_pause["end"].startswith("2024-01-01T12:00:00")


def test_merged_timeline_counts_inter_member_gap_as_pause_and_merges_adjacent(client, login_as):
    """The inter-member tracking gap is treated as pause time (matching how
    the merged ride's own total_pauses_duration is computed: merged duration
    minus each member's own riding time, which already counts that gap as
    pause). If a member's own trailing segment is also a pause, they must
    render as a single continuous block, not two adjacent slivers."""
    login_as(client, "user-1")

    # Member 1 ends already paused (dangling pause, no resume) — the 15min
    # gap before member 2 starts should merge into this same pause segment.
    _seed_ride(
        "user-1", "r1", "2024-01-01T10:00:00Z",
        duration=1800.0, duration_without_pauses=900.0, total_pauses_duration=900.0, pause_count=1,
        pauses=[make_event("2024-01-01T10:15:00Z")],
    )
    _seed_ride(
        "user-1", "r2", "2024-01-01T10:45:00Z",
        duration=1800.0, duration_without_pauses=1800.0, total_pauses_duration=0.0, pause_count=0,
    )

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200
    rep_id = resp.json()["id"]

    timeline = client.get(f"/api/rides/{rep_id}").json()["timeline"]

    pause_segments = [s for s in timeline if s["type"] == "pause"]
    assert len(pause_segments) == 1
    assert pause_segments[0]["start"].startswith("2024-01-01T10:15:00")
    assert pause_segments[0]["end"].startswith("2024-01-01T10:45:00")  # merged through to member 2's start
