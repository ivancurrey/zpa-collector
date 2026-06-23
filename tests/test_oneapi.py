import io
import json
import urllib.error
import urllib.parse

import pytest

from collector.config import Settings
from collector.oneapi import OneAPIClient, OneAPIError


def _settings():
    return Settings(
        zs_vanity_domain="acme", zs_client_id="id", zs_client_secret="sec",
        zpa_customer_id="123", dash_token="t",
    )


class FakeResp:
    """Mimics the object returned by OpenerDirector.open (context manager)."""
    def __init__(self, body, status=200, headers=None):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Injectable stand-in for urllib.request.build_opener()."""
    def __init__(self, script):
        self._script = script
        self.calls = []  # list of (method, url)

    def open(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        self.calls.append((method, url))
        return self._script(method, url, self.calls)


def _http_error(url, code, retry_after=None):
    hdrs = {}
    if retry_after is not None:
        hdrs["retry-after"] = str(retry_after)
    return urllib.error.HTTPError(url, code, "err", hdrs, io.BytesIO(b"boom"))


def test_token_parses_and_caches(monkeypatch):
    token_hits = {"n": 0}

    def script(method, url, calls):
        if url.endswith("/oauth2/v1/token"):
            token_hits["n"] += 1
            return FakeResp(json.dumps({"access_token": "TOK-1"}))
        raise AssertionError(f"unexpected url {url}")

    opener = FakeOpener(script)
    client = OneAPIClient(_settings(), opener=opener)
    assert client.token() == "TOK-1"
    assert client.token() == "TOK-1"  # cached, no second POST
    assert token_hits["n"] == 1
    assert opener.calls[0][0] == "POST"
    assert opener.calls[0][1] == "https://acme.zslogin.net/oauth2/v1/token"


def test_token_missing_access_token_raises():
    def script(method, url, calls):
        return FakeResp(json.dumps({"nope": 1}))
    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    with pytest.raises(OneAPIError):
        client.token()


def _token_or(url, calls, then):
    if url.endswith("/oauth2/v1/token"):
        return FakeResp(json.dumps({"access_token": "TOK"}))
    return then(url, calls)


def test_paged_get_follows_total_pages_and_merges():
    def then(url, calls):
        q = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(q)
        page = int(params["page"][0])
        assert params["pagesize"][0] == "500"
        if page == 1:
            return FakeResp(json.dumps({"list": [{"id": "a"}], "totalPages": "2"}))
        return FakeResp(json.dumps({"list": [{"id": "b"}], "totalPages": "2"}))

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    rows = client.paged_get("application")
    assert [r["id"] for r in rows] == ["a", "b"]


def test_paged_get_retries_429_then_succeeds(monkeypatch):
    import collector.oneapi as oneapi_mod
    slept = []
    monkeypatch.setattr(oneapi_mod.time, "sleep", lambda s: slept.append(s))
    state = {"n": 0}

    def then(url, calls):
        state["n"] += 1
        if state["n"] == 1:
            raise _http_error(url, 429, retry_after=7)
        return FakeResp(json.dumps({"list": [{"id": "x"}], "totalPages": "1"}))

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    rows = client.paged_get("segmentGroup")
    assert [r["id"] for r in rows] == ["x"]
    assert slept and slept[0] >= 7  # honored retry-after


def test_paged_get_omits_microtenant_when_none():
    seen = {}

    def then(url, calls):
        seen["url"] = url
        return FakeResp(json.dumps({"list": [], "totalPages": "1"}))

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    client.paged_get("server", microtenant_id=None)
    assert "microtenantId" not in seen["url"]


def test_paged_get_includes_microtenant_when_set():
    seen = {}

    def then(url, calls):
        seen["url"] = url
        return FakeResp(json.dumps({"list": [], "totalPages": "1"}))

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    client.paged_get("server", microtenant_id="mt-1")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert params["microtenantId"][0] == "mt-1"


def test_list_microtenants_returns_none_on_404():
    def then(url, calls):
        raise _http_error(url, 404)

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    assert client.list_microtenants() == [None]


def test_list_microtenants_prefixes_none_then_ids():
    def then(url, calls):
        return FakeResp(json.dumps(
            {"list": [{"id": "mt-1"}, {"id": "mt-2"}], "totalPages": "1"}))

    def script(method, url, calls):
        return _token_or(url, calls, then)

    client = OneAPIClient(_settings(), opener=FakeOpener(script))
    assert client.list_microtenants() == [None, "mt-1", "mt-2"]
