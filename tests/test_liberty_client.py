"""liberty_client._post_with_retry — retry-with-backoff on transient 5xx
errors from Liberty Rider's API (previously had zero test coverage)."""
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

    def post(self, url, json, timeout):
        self.calls += 1
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
