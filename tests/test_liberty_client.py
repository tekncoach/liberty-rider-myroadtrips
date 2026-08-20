"""liberty_client — retry-with-backoff on transient 5xx errors from Liberty
Rider's API (previously had zero test coverage), plus the cheap
`get_latest_ride` probe behind /api/sync/status."""
import requests

import liberty_client as lc


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.posts = []

    def post(self, url, json, timeout):
        self.calls += 1
        self.posts.append(json)
        return self._responses[self.calls - 1]


def test_succeeds_on_first_try(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(200, {"ok": True})])
    resp = lc._post_with_retry(session, "http://x", {}, timeout=5)
    assert resp.json() == {"ok": True}
    assert session.calls == 1


def test_retries_then_succeeds_on_transient_502(monkeypatch):
    sleeps = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    session = FakeSession([FakeResponse(502), FakeResponse(502), FakeResponse(200, {"ok": True})])
    resp = lc._post_with_retry(session, "http://x", {}, timeout=5)
    assert resp.json() == {"ok": True}
    assert session.calls == 3
    assert len(sleeps) == 2  # backoff before each retry, none after final success


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(502)] * (lc.MAX_RETRIES + 1))
    try:
        lc._post_with_retry(session, "http://x", {}, timeout=5)
        assert False, "expected HTTPError"
    except requests.HTTPError:
        pass
    assert session.calls == lc.MAX_RETRIES + 1


def test_non_retryable_status_raises_immediately(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(401), FakeResponse(200, {"ok": True})])
    try:
        lc._post_with_retry(session, "http://x", {}, timeout=5)
        assert False, "expected HTTPError"
    except requests.HTTPError:
        pass
    assert session.calls == 1  # never touched the second (would-be-successful) response


def _client_with(session):
    client = lc.LibertyRiderClient("tok")
    client.session = session
    return client


def _rides_payload(rides):
    return {"data": {"currentUser": {"id": "user-1", "stoppedRides": rides}}}


def test_get_latest_ride_returns_the_newest_ride():
    # A page comes back ascending, and the server has been seen to ignore a
    # small `first` — so the newest is picked, not simply the first element.
    session = FakeSession([FakeResponse(200, _rides_payload([
        {"id": "old", "startTime": "2026-07-01T09:00:00.000Z"},
        {"id": "new", "startTime": "2026-07-05T17:33:29.243Z"},
    ]))])
    assert _client_with(session).get_latest_ride() == {"id": "new", "startTime": "2026-07-05T17:33:29.243Z"}


def test_get_latest_ride_returns_none_when_the_account_has_no_rides():
    session = FakeSession([FakeResponse(200, _rides_payload([]))])
    assert _client_with(session).get_latest_ride() is None


def test_get_latest_ride_raises_on_graphql_error():
    session = FakeSession([FakeResponse(200, {"data": None, "errors": [{"message": "nope"}]})])
    try:
        _client_with(session).get_latest_ride()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "nope" in str(e)


def test_get_latest_ride_asks_for_a_single_ride_and_nothing_heavy():
    session = FakeSession([FakeResponse(200, _rides_payload([]))])
    _client_with(session).get_latest_ride()
    payload = session.posts[0]
    assert payload["variables"] == {"first": 1}
    assert "detailedPolyline" not in payload["query"]
